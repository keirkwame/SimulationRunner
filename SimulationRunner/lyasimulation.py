"""Specialization of the Simulation class to Lyman-alpha forest simulations."""

import os
import numpy as np
import scipy.interpolate as interp
from . import simulationics

class LymanAlphaSim(simulationics.SimulationICs):
    """Specialise the Simulation class for the Lyman alpha forest.
       This uses the QuickLya star formation module with sigma_8 and n_s.
    """
    __doc__ = __doc__+simulationics.SimulationICs.__doc__
    def __init__(self, *, rescale_gamma = True, rescale_amp = 1., rescale_slope = -0.0, redend = 2.2, uvb="pu", **kwargs):
        #Parameters of the heating rate rescaling to account for helium reionisation
        #Default parameters do nothing
        self.rescale_gamma = rescale_gamma
        self.rescale_amp = rescale_amp
        self.rescale_slope = rescale_slope
        super().__init__(redend=redend, uvb=uvb, **kwargs)
        assert self.separate_gas

    def _feedback_config_options(self, config, prefix=""):
        """Config options specific to the Lyman alpha forest star formation criterion"""
        #No feedback!
        return

    def _feedback_params(self, config):
        """Config options specific to the lyman alpha forest"""
        #These are parameters for the Quick Lyman alpha star formation.
        config["QuickLymanAlphaProbability"] = 1.0
        #No FOF for lya.
        config['SnapshotWithFOF'] = 0
        #Quick star formation threshold from 1605.03462
        config["CritOverDensity"] = 1000.
        config['WindModel'] = 'nowind'
        #These are parameters for the model to rescale the temperature-density relation
        if self.rescale_gamma:
            config["HeliumHeatOn"] = 1
            config["HeliumHeatThresh"] = 10.0
            config["HeliumHeatAmp"]  = self.rescale_amp
            config["HeliumHeatExp"] = self.rescale_slope
        return config

    def generate_times(self):
        """Snapshot outputs for lyman alpha"""
        redshifts = np.arange(4.2, self.redend, -0.2)
        return 1./(1.+redshifts)


class LymanAlphaNCDMSim(LymanAlphaSim):
    """Specialise the LymanAlphaSim class for nCDM simulations."""
    def __init__(self, *, alpha, beta, gamma, rescale_gamma=True, rescale_amp=1., rescale_slope=0., redend=4.24,
                 uvb='on', **kwargs):
        self.alpha = alpha
        self.beta = beta
        self.gamma = gamma
        super().__init__(rescale_gamma=rescale_gamma, rescale_amp=rescale_amp, rescale_slope=rescale_slope,
                         redend=redend, uvb=uvb, **kwargs)

    def generate_times(self):
        """Decide which snapshots to save"""
        snapshot_redshifts = np.array([7., 6., 5.4, 4.95, 4.58, 4.4, 4.24])
        return 1. / (1. + snapshot_redshifts)

    def transfer_function_nCDM(self, k, alpha=0., beta=1., gamma=0.):
        """Square root of ratio of linear power spectrum in presence of nCDM with respect to that in presence of CDM."""
        return (1. + ((alpha * k) ** beta)) ** gamma

    def get_linear_matter_power_spectrum(self, k, z, power_spectrum_instance):
        """Get linear matter power spectrum for given k (h/Mpc) and z."""
        nCDM_correction = self.transfer_function_nCDM(k, alpha=self.alpha, beta=self.beta, gamma=self.gamma) ** 2
        return power_spectrum_instance.get_pklin(k=k, z=z) * nCDM_correction


class LymanAlphaKnotICs(LymanAlphaSim):
    """Specialise the generation of initial conditions to change the power spectrum via knots.
    knot_val is a multiplicative factor applied to the power spectrum at knot_pos
    knot_pos is in units of the k bins for the power spectrum output by CAMB, by default h/Mpc."""
    def __init__(self, *, knot_pos= (0.15,0.475,0.75,1.19), knot_val = (1.,1.,1.,1.), **kwargs):
        #Set up the knot parameters
        self.knot_pos = knot_pos
        self.knot_val = knot_val
        super().__init__(**kwargs)

    def _alter_power(self, camb_output):
        """Generate a new CAMB power spectrum multiplied by the knot values."""
        camb_file = camb_output+"_matterpow_"+str(self.redshift)+".dat"
        matpow = np.loadtxt(camb_file)
        matpow2 = change_power_spectrum_knots(self.knot_pos, self.knot_val, matpow)
        #Save a copy of the old file
        os.rename(camb_file, camb_file+".orig")
        np.savetxt(camb_file, matpow2)
        return

def change_power_spectrum_knots(knotpos, knotval, matpow):
    """Multiply the power spectrum file by a function specified by our knots.
    We assume that the power spectrum is linearly interpolated between the knots,
    so that we preserve additivity:
    ie, P(k | A =1.1, B=1.1) / P(k | A =1, B=1) == P(k | A =1.1) / P(k | A =1.)+ P(k | B =1.1) / P(k | A =B.)
    On scales larger and smaller than the specified knots, the power spectrum is changed by the same factor as the last knot specified.
    So if the smallest knotval is 1.1, P(k) from k = 0 -> knotpos[0] is multiplied by 1.1.
    Note that this means that if you want the large scales to be unchanged, you should impose an extra, fixed, knot that stays constant."""
    #This should catch some cases where we pass the arguments in the wrong order
    assert np.all([k1 < k1p for (k1, k1p) in zip(knotpos[:-1], knotpos[1:])])
    assert np.shape(knotval) == np.shape(knotpos)
    #Split and copy the matter power spectrum
    kval = np.array(matpow[:,0])
    pval = np.array(matpow[:,1])
    #Check that the input makes physical sense
    assert np.all(knotpos) > 0
    assert np.all(knotpos) > kval[0] and np.all(knotpos) < kval[-1]
    assert np.all(knotval) > 0
    #BOUNDARY CONDITIONS
    #Add knots at the start and end of the matter power spectrum.
    #The large scale knot is always 1.
    #The small-scale knot always follows the last real knot
    ext_knotpos = np.concatenate([[kval[0]*0.95,],knotpos, [kval[-1]*1.05,] ])
    ext_knotval = np.concatenate([[knotval[0],],knotval, [knotval[-1],] ])
    assert np.shape(ext_knotpos) == np.shape(ext_knotval) and np.shape(ext_knotpos) == (np.size(knotval)+2,)
    #Insert extra power spectrum evaluations at each knot, to make sure we capture those points properly.
    #Build an interpolator (in log space) to get new Pk values. Only interpolate a subset of Pk for speed
    i_limits = np.searchsorted(kval, [knotpos[0]*0.66, knotpos[-1]*1.5])
    (imin, imax) = (np.max([0,i_limits[0]-5]), np.min([len(kval)-1,i_limits[-1]+5]))
    pint = interp.interp1d(np.log(kval[imin:imax]), np.log(pval[imin:imax]), kind='cubic')
    #Also add an extra point in the midpoint of their interval. This helps spline interpolators.
    locations = np.searchsorted(kval[imin:imax], knotpos)
    midpoints = (kval[imin:imax][locations] + kval[imin:imax][locations-1])/2.
    kplocs = np.searchsorted(knotpos, midpoints)
    ins_knotpos = np.insert(knotpos, kplocs, midpoints)
    #Now actually add the new points to the Pk array
    index = np.searchsorted(kval, ins_knotpos)
    kval = np.insert(kval, index, ins_knotpos)
    pval = np.insert(pval, index, np.exp(pint(np.log(ins_knotpos))))
    #Check for very close entries and remove them
    collision = np.where(np.abs(kval[1:] - kval[:-1]) < 1e-5 * kval[1:])
    if np.size(collision) > 0:
        kval = np.delete(kval,collision)
        pval = np.delete(pval,collision)
    #Check we didn't add the same row twice.
    assert np.size(np.unique(kval)) == np.size(kval)
    #Linearly interpolate between these values
    dpk = interp.interp1d(ext_knotpos, ext_knotval, kind='linear')
    #Multiply by the knotted power spectrum interpolated to the point given in the power spectrum file.
    pval *= dpk(kval)
    #Build something like the original matpow
    return np.vstack([kval, pval]).T

if __name__ == "__main__":
    ss = LymanAlphaKnotICs(knot_val = (1.,1.2,1.,1.),outdir=os.path.expanduser("~/data/Lya_Boss/test3"), box=60, npart=512)
    ss.make_simulation()
