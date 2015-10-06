"""
Module to automate the generation of simulation config files.
The base class is Simulation, which creates the config files for a single simulation.
It is meant to be called from other classes as part of a suite,
More specialised simulation types can inherit from it.
Different machines can be implemented as decorators.
"""
from __future__ import print_function
import os.path
import re
import configobj
import math
import numpy as np
import shutil
import glob
import read_uvb_tab
import subprocess

def find_exec(executable):
    """Simple function to locate a binary in a nearby directory"""
    possible = [executable,]+ glob.glob(os.path.join("../*/", executable))
    exists = [ex for ex in possible if os.path.exists(ex) and os.path.isfile(ex) ]
    if len(exists) > 1:
        print("Warning: found multiple possibilities: ",exists)
    if len(exists) > 0:
        return exists[0]
    raise ValueError(executable+" not found")

class Simulation(object):
    """
    Class for creating config files needed to run a single simulation.
    There are a few things this class needs to do:

    - Generate CAMB input files
    - Generate N-GenIC input files (to use CAMB output)
    - Run CAMB and N-GenIC to generate ICs
    - Generate Gadget input files that match the ICs

    The class will store the parameters of the simulation, and each public method will do one of these things.
    Many things are left hard-coded.
    We assume flatness.

    Init parameters:
    outdir - Directory in which to save ICs
    box - Box size in comoving Mpc/h
    npart - Cube root of number of particles
    separate_gas - if true the ICs will contain baryonic particles. If false, just DM.
    redshift - redshift at which to generate ICs
    omegab - baryon density. Note that if we do not have gas particles, still set omegab, but set separate_gas = False
    omegam - Matter density
    hubble - Hubble parameter, h, which is H0 / (100 km/s/Mpc)
    scalar_amp - Initial amplitude of scalar power spectrum to feed to CAMB
    ns - tilt of scalar power spectrum to feed to CAMB
    """
    def __init__(self, outdir, box, npart, nproc, memory, timelimit, seed = 9281110, redshift=99, redend = 0, separate_gas=True, omegac=0.2408, omegab=0.0472, hubble=0.7, scalar_amp=2.427e-9, ns=0.97, uvb="hm"):
        #Check that input is reasonable and set parameters
        #In Mpc/h
        assert box < 20000
        self.box = box
        #Cube root
        assert npart > 1 and npart < 16000
        self.npart = npart
        #Physically reasonable
        assert omegac <= 1 and omegac > 0
        self.omegac = omegac
        assert omegab > 0 and omegab < 1
        self.omegab = omegab
        assert redshift > 1 and redshift < 1100
        self.redshift = redshift
        assert redend >= 0 and redend < 1100
        self.redend = redend
        assert hubble < 1 and hubble > 0
        self.hubble = hubble
        assert scalar_amp < 1e-8 and scalar_amp > 0
        self.scalar_amp = scalar_amp
        assert ns > 0 and ns < 2
        self.ns = ns
        #Structure seed.
        self.seed = seed
        #Baryons?
        self.separate_gas = separate_gas
        #UVB? Only matters if gas
        self.uvb = uvb
        assert self.uvb == "hm" or self.uvb == "fg"
        self.omeganu = 0
        #CPU parameters
        self.nproc = nproc
        self.email = "sbird4@jhu.edu"
        self.timelimit = timelimit
        #Maximum memory available for an MPI task
        self.memory = memory
        #Number of files per snapshot
        #This is chosen to give a reasonable number and
        #a constant number of particles per file.
        self.numfiles = np.max([1,self.npart**3/2**24])
        #Maximum number of files to write in parallel.
        #Cannot be larger than number of processors
        self.maxpwrite = nproc
        #Total matter density
        self.omega0 = self.omegac + self.omegab + self.omeganu
        outdir = os.path.expanduser(outdir)
        #Make the output directory: will fail if parent does not exist
        if not os.path.exists(outdir):
            os.mkdir(outdir)
        else:
            if os.listdir(outdir) != []:
                print("Warning: ",outdir," is non-empty")
        self.outdir = outdir
        #Default values for the CAMB parameters
        self.cambdefault = "params.ini"
        #Filename for new CAMB file
        self.cambout = "_camb_params.ini"
        #Default GenIC paths
        self.genicdefault = "ngenic.param"
        self.genicout = "_genic_params.ini"
        #Default parameter file names
        self.gadgetdefaultparam = "gadgetparams.param"
        self.gadgetparam = "gadget3.param"
        #Executable names
        self.cambexe = "camb"
        self.gadgetexe = "P-Gadget3"
        self.gadgetconfig = "Config.sh"
        self.gadget_dir = os.path.expanduser("~/codes/P-Gadget3/")
        self.genicexe = "N-GenIC"

    def cambfile(self):
        """Generate the CAMB parameter file from the (cosmological) simulation parameters and the default values"""
        #Load CAMB file using ConfigObj
        config = configobj.ConfigObj(self.cambdefault)
        config.filename = os.path.join(self.outdir, self.cambout)
        #Set values
        camb_output = os.path.join(self.outdir,"camb_linear")+"/ics_"
        config['output_root'] = camb_output
        #Can't change this easily because the parameters then have different names
        assert config['use_physical'] == 'T'
        config['hubble'] = self.hubble * 100
        config['ombh2'] = self.omegab*self.hubble**2
        config['omch2'] = self.omegac*self.hubble**2
        config['omk'] = 0.
        #Initial power spectrum: MAKE SURE you set the pivot scale to the WMAP value!
        config['pivot_scalar'] = 2e-3
        config['pivot_tensor'] = 2e-3
        config['scalar_specral_index(1)'] = self.ns
        config['scalar_specral_amp(1)'] = self.scalar_amp
        #Various numerical parameters
        #Maximum relevant scale is 2 pi * softening length. Use a kmax double that for safety.
        config['transfer_kmax'] = 2*math.pi*100*self.npart/self.box
        #At which redshifts should we produce CAMB output: we want the starting redshift of the simulation,
        #but we also want some other values for checking purposes
        redshifts = [self.redshift, (self.redshift+1)/2-1] + [9,4,2,1,0]
        for (n,zz) in zip(range(len(redshifts)), redshifts):
            config['transfer_redshift('+str(n)+')'] = zz
            config['transfer_filename('+str(n)+')'] = 'transfer_'+str(zz)+'.dat'
            config['transfer_matterpower('+str(n)+')'] = 'matterpow_'+str(zz)+'.dat'
        #Set up the neutrinos.
        #This has it's own function so it can be overriden by child classes
        config = self._camb_neutrinos(config)
        #Write the config file
        config.write()
        return (camb_output, config.filename)

    def _camb_neutrinos(self, config):
        """Modify the CAMB config file to have massless neutrinos.
        Designed to be easily over-ridden"""
        config['massless_neutrinos'] = 3.046
        config['massive_neutrinos'] = 0
        return config

    def genicfile(self, camb_output):
        """Generate the GenIC parameter file"""
        config = configobj.ConfigObj(self.genicdefault)
        config.filename = os.path.join(self.outdir, self.genicout)
        config['Box'] = self.box*1000
        config['Nsample'] = self.npart
        config['Nmesh'] = self.npart * 3/2
        genicout = "ICS"
        config['OutputDir'] = os.path.join(self.outdir, genicout)
        #Is this enough information, or should I add a short hash?
        genicfile = str(self.box)+"_"+str(self.npart)+"_"+str(self.redshift)
        config['FileBase'] = genicfile
        #Whether we have baryons is entirely controlled by the glass file.
        #Since the glass file is just a regular grid, this should probably be in GenIC at some point
        if self.separate_gas:
            config['GlassFile'] = os.path.expanduser("~/data/glass/reg-grid-128-2comp")
        else:
            config['GlassFile'] = os.path.expanduser("~/data/glass/reg-grid-128-dm")
        config['GlassTileFac'] = self.npart/128
        #Total matter density, not CDM matter density.
        config['Omega'] = self.omega0
        config['OmegaLambda'] = 1- self.omega0
        config['OmegaBaryon'] = self.omegab
        config['OmegaDM_2ndSpecies'] = self.omeganu
        config['HubbleParam'] = self.hubble
        config['Redshift'] = self.redshift
        config['FileWithInputSpectrum'] = camb_output + "matterpow_"+str(self.redshift)+".dat"
        config['FileWithTransfer'] = camb_output + "transfer_"+str(self.redshift)+".dat"
        config['NumFiles'] = self.numfiles
        assert config['InputSpectrum_UnitLength_in_cm'] == '3.085678e24'
        config = self._genicfile_neutrinos(config)
        config['Seed'] = self.seed
        config.write()
        return (os.path.join(genicout, genicfile), config.filename)

    def _genicfile_neutrinos(self, config):
        """Neutrino parameters easily overridden"""
        config['NU_On'] = 0
        return config

    def gadget3config(self):
        """Generate a Gadget Config.sh file. This doesn't fit nicely into configobj.
        Many of the simulation parameters are stored here, but none of the cosmology.
        Some of these parameters are cluster dependent.
        We are assuming Gadget-3. Arepo or Gadget-2 need a different set of options."""
        g_config_filename = os.path.join(self.outdir, self.gadgetconfig)
        with open(g_config_filename) as config:
            config.write("PERIODIC")
            #Can be reduced for lower memory but lower speed.
            config.write("PMGRID="+self.npart*2)
            #These are memory options: if short on memory, change them.
            config.write("MULTIPLEDOMAINS=4")
            config.write("TOPNODEFACTOR=3.0")
            #Again, can be turned off for lower memory usage
            #but changes output format
            config.write("LONGIDS")
            config.write("PEANOHILBERT")
            config.write("WALLCLOCK")
            config.write("MYSORT")
            config.write("MOREPARAMS")
            config.write("POWERSPEC_ON_OUTPUT")
            config.write("POWERSPEC_ON_OUTPUT_EACH_TYPE")
            #isend/irecv is quite slow on some clusters because of the extra memory allocations.
            #Maybe test this on your specific system and see if it helps.
            config.write("NO_ISEND_IRECV_IN_DOMAIN")
            config.write("NO_ISEND_IRECV_IN_PM")
            #Changes H(z)
            config.write("INCLUDE_RADIATION")
            config.write("HAVE_HDF5")
            #We may need this sometimes, depending on the machine
            #config.write("NOTYPEPREFIX_FFTW")
            #Options for gas simulations
            if self.separate_gas:
                config.write("COOLING")
                #This needs implementing
                #config.write("UVB_SELF_SHIELDING")
                #Optional feedback model options
                self._feedback_config_options(config)
        return g_config_filename

    def _feedback_config_options(self, config):
        """Options in the Config.sh file for a potential star-formation/feedback model"""
        config.write("USE_SFR")
        return

    def gadget3params(self, genicfileout):
        """Gadget 3 parameter file. Almost a configobj, but needs a regex at the end to change # to % and remove '='.
        Again, will be different for Arepo and Gadget2.
        Arguments:
            genicfileout - where the ICs are saved
            timelimit - simulation time limit in hours"""
        config = configobj.ConfigObj(self.gadgetdefaultparam)
        config.filename = os.path.join(self.outdir, self.gadgetparam)
        config['InitCondFile'] = genicfileout
        config['OutputDir'] = "output"
        config['SnapshotFileBase'] = "snap"
        config['TimeLimitCPU'] = 60*60*self.timelimit*20/17.-3000
        config['TimeBegin'] = 1./(1+self.redshift)
        config['TimeMax'] = 1./(1+self.redend)
        config['Omega0'] = self.omega0
        config['OmegaLambda'] = 1- self.omega0
        #OmegaBaryon should be zero for gadget if we don't have gas particles
        config['OmegaBaryon'] = self.omegab*self.separate_gas
        config['HubbleParam'] = self.hubble
        config['BoxSize'] = self.box * 1000
        config['OutputListOn'] = 1
        timefile = "times.txt"
        config['OutputListFilenames'] = timefile
        self._print_times(timefile)
        #This should just be larger than the simulation time limit
        config['CpuTimeBetRestartFile'] = 60*60*self.timelimit*10
        config['NumFilesPerSnapshot'] = self.numfiles
        #There is a maximum here because some filesystems may not like parallel writes!
        config['NumFilesWrittenInParallel'] = np.min([self.maxpwrite, self.numfiles])
        #Softening is 1/30 of the mean linear interparticle spacing
        soften = self.box/self.npart/30.
        for ptype in ('Gas', 'Halo', 'Disk', 'Bulge', 'Stars', 'Bndry'):
            config['Softening'+ptype] = soften
            config['Softening'+ptype+'MaxPhys'] = soften
        config['ICFormat'] = 3
        config['SnapFormat'] = 3
        config['RestartFile'] = "restartfiles/restart"
        #This could be tuned in lower memory conditions
        config['BufferSize'] = 100
        if self.separate_gas:
            config['CoolingOn'] = 1
            config = self._sfr_params(config)
            config = self._feedback_params(config)
            #Copy a TREECOOL file into the right place.
            self._copy_uvb()
            #Need more memory for a feedback model
            config['PartAllocFactor'] = 4
        else:
            config['PartAllocFactor'] = 2
        config['MaxMemSize'] = self.memory
        #Add other config parameters
        config = self._other_params(config)
        config.write()
        #Now we need to regex the generated file to fit the gadget format
        #This is somewhat unsafe, but who cares?
        cf = open(config.filename,'r')
        configstr = cf.read()
        configstr = re.sub("#","%",configstr)
        configstr = re.sub("="," ",configstr)
        cf.close()
        cf = open(config.filename,'w')
        cf.write(configstr)
        cf.close()
        return

    def _sfr_params(self, config):
        """Config parameters for the default Springel & Hernquist star formation model"""
        config['StarFormationOn'] = 1
        config['CritPhysDensity'] =  0
        config['MaxSfrTimescale'] = 1.5
        config['CritOverDensity'] = 1000.0
        config['TempSupernova'] = 1e+08
        config['TempClouds'] = 1000
        config['FactorSN'] = 0.1
        config['FactorEVP'] = 1000
        return config

    def _feedback_params(self, config):
        """Config parameters for the feedback models"""
        return config

    def _other_params(self, config):
        """Function to override to set other config parameters"""
        return config

    def _generate_times(self):
        """List of output times for a simulation. Can be overridden,
        but default is evenly spaced in a from start to end."""
        astart = 1./(1+self.redshift)
        aend = 1./(1+self.redend)
        times = np.linspace(astart, aend,9)
        return times

    def _copy_uvb(self):
        """The UVB amplitude for Gadget is specified in a file named TREECOOL in the same directory as the gadget binary."""
        fuvb = read_uvb_tab.get_uvb_filename(self.uvb)
        shutil.copy(fuvb, os.path.join(self.outdir,"TREECOOL"))

    def _print_times(self, timefile):
        """Print times to the times.txt file"""
        times = self._generate_times()
        with open(os.path.join(self.outdir, timefile),'w') as timetxt:
            timetxt.write(times)

    def generate_mpi_submit(self):
        """Generate a sample mpi_submit file.
        The prefix argument is a string at the start of each line.
        It separates queueing system directives from normal comments"""
        with open(os.path.join(self.outdir, "mpi_submit"),'w') as mpis:
            mpis.write("#!/bin/bash")
            mpis.write(self._queue_directive())
            mpis.write("mpirun -np "+self.nproc+" "+self.gadgetexe+" "+self.gadgetparam)

    def _queue_directive(self, prefix="#PBS"):
        """Write the part of the mpi_submit file that directs the queueing system.
        This is usually specific to a given cluster.
        The prefix argument is a string at the start of each line.
        It separates queueing system directives from normal comments"""
        qstring = prefix+" -j eo\n"
        qstring += prefix+" -m bae\n"
        qstring += prefix+" -M "+self.email+"\n"
        qstring += prefix+" -l walltime="+self.timelimit+":00:00\n"
        return qstring

    def make_simulation(self):
        """Wrapper function to make all the simulation parameter files in turn and run the binaries"""
        #First generate the input files for CAMB
        (camb_output, camb_param) = self.cambfile()
        #Then run CAMB
        camb = find_exec("camb")
        #In python 3.5, can use subprocess.run to do this.
        #But for backwards compat, use check_output
        self.camb_stdout = subprocess.check_output([camb, camb_param])
        #Now generate the GenIC parameters
        (genic_output, genic_param) = self.genicfile(camb_output)
        #Run N-GenIC
        genic = find_exec("N-GenIC")
        self.genic_stdout = subprocess.check_output([genic, genic_param])
        #Generate Gadget makefile
        gadget_config = self.gadget3config()
        #Symlink the new gadget config to the source directory
        os.remove(os.path.join(self.gadget_dir, self.gadgetconfig))
        os.symlink(gadget_config, os.path.join(self.gadget_dir, self.gadgetconfig))
        #Build gadget
        gadget_binary = os.path.join(self.gadget_dir, self.gadgetexe)
        g_mtime = os.stat(gadget_binary).st_mtime
        self.make_stdout = subprocess.check_output(["make", "-j4"], cwd=self.gadget_dir)
        #Check that the last-changed time of the binary has actually changed..
        assert g_mtime != os.stat(gadget_binary).st_mtime
        #Copy the gadget binary to the new location
        shutil.copy(os.path.join(self.gadget_dir, self.gadgetexe), os.path.join(self.outdir,self.gadgetexe))
        #Generate Gadget parameter file
        self.gadget3params(genic_output)
        #Generate mpi_submit file
        self.generate_mpi_submit()

#This decorator (function which acts on a function) contains the information
#specific to using the COMA cluster.
def coma_mpi_decorate(que_str):
    """Decorate an mpi_submit function for a given cluster"""
    def new_que_str(self, prefix="#PBS"):
        """Generate mpi_submit with coma specific parts"""
        qstring = que_str(self, prefix)
        qstring += prefix+" -q amd\n"
        qstring += prefix+" -l nodes="+self.nproc/16+":ppn=16\n"
        return qstring
    return new_que_str

