#!/usr/bin/env python
# Copyright 2014-2019 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Authors: Qiming Sun <osirpt.sun@gmail.com>
#

'''
Range separation JK builder
'''

import time
import copy
import ctypes
import numpy as np
from pyscf import gto
from pyscf import lib
from pyscf.lib import logger
from pyscf.scf import _vhf
from pyscf.pbc.df import aft
from pyscf.pbc import tools as pbctools
from pyscf.pbc.lib.kpts_helper import gamma_point
from pyscf import __config__

libpbc = lib.load_library('libpbc')

class RangeSeparationJKBuilder(object):
    def __init__(self, cell, kpts=np.zeros((1,3))):
        self.cell = cell
        self.stdout = cell.stdout
        self.verbose = cell.verbose
        self.max_memory = cell.max_memory
        self.mesh = None
        self.kpts = kpts

        self.omega = 0.5
        self.cell_dense = None
        self.cell_sparse = None
        # Born-von Karman supercell
        self.bvkcell = None
        self.bvkmesh_Ls = None
        self.phase = None
        self.supmol = None
        self.supmol_dense = None
        self.supmol_sparse = None
        # For shells in the supmol, bvkcell_shl_id is the shell ID in bvkcell 
        self.bvkcell_shl_id = None
        self.ovlp_mask = None
        self.lr_aft = None
        self.sr_aft = None
        self.ke_cutoff = None
        self.vhfopt = None

        # to mimic molecular DF object
        self.blockdim = getattr(__config__, 'pbc_df_df_DF_blockdim', 240)

        self._keys = set(self.__dict__.keys())

    def reset(self):
        pass

    def dump_flags(self, verbose=None):
        logger.info(self, '\n')
        logger.info(self, '******** %s ********', self.__class__)
        logger.info(self, 'mesh = %s (%d PWs)', self.mesh, np.prod(self.mesh))
        logger.info(self, 'omega = %s', self.omega)
        #logger.info(self, 'len(kpts) = %d', len(self.kpts))
        #logger.debug1(self, '    kpts = %s', self.kpts)
        return self

    def reset(self, cell=None):
        if cell is not None:
            self.cell = cell
        self.supmol = None
        return self

    def build(self, omega=None, direct_scf_tol=None):
        from pyscf.pbc.tools import k2gamma
        cpu0 = (time.clock(), time.time())
        cell = self.cell
        kpts = self.kpts

        if omega is not None:
            self.omega = omega

        if self.omega is None:
            # Search a proper range-separation parameter omega that can balance the
            # computational cost between the real space integrals and moment space
            # integrals
            self.ke_cutoff = min(pbctools.mesh_to_cutoff(cell.lattice_vectors(),
                                                         self.mesh[:cell.dimension]))
            self.omega = aft.estimate_omega_for_ke_cutoff(cell, self.ke_cutoff)
        else:
            self.ke_cutoff = aft.estimate_ke_cutoff_for_omega(cell, self.omega)
            self.mesh = pbctools.cutoff_to_mesh(cell.lattice_vectors(), self.ke_cutoff)

        logger.info(self, 'omega = %.15g  ke_cutoff = %s  mesh = %s',
                    self.omega, self.ke_cutoff, self.mesh)

        if direct_scf_tol is None:
            direct_scf_tol = cell.precision * 1e-3
            logger.debug(self, 'Set direct_scf_tol %g', direct_scf_tol)

        kmesh = k2gamma.kpts_to_kmesh(cell, kpts)
        bvkcell, phase = k2gamma.get_phase(cell, kpts, kmesh)
        self.bvkmesh_Ls = Ks = k2gamma.translation_vectors_for_kmesh(cell, kmesh)
        self.bvkcell = bvkcell
        self.phase = phase

        # Given ke_cutoff, eta corresponds to the most steep Gaussian basis
        # of which the Coulomb integrals can be accurately computed in moment
        # space. cell_sparse is the cell of smooth functions. Their exponents
        # are ~< eta.
        eta = aft.estimate_eta_for_ke_cutoff(cell, self.ke_cutoff,
                                             precision=direct_scf_tol)
        # * Assuming the most smooth function in cell_dense has exponent eta,
        # with attenuation parameter omega, rcut_sr is the distance of which
        # the value of attenuated Coulomb integrals of four shells |eta> is
        # smaller than the required precision.
        # * The attenuated coulomb integrals between four s-type Gaussians
        # (2*a/pi)^{3/4}exp(-a*r^2) is
        #   (erfc(omega*a^0.5/(omega^2+a)^0.5*R) - erfc(a^0.5*R)) / R
        # if two Gaussians on one center and the other two on another center
        # and the distance between the two centers are R.
        # * The attenuated coulomb integrals between two spherical charge
        # distributions is
        #   ~(pi/eta)^3/2 (erfc(tau*(eta/2)^0.5*R) - erfc((eta/2)^0.5*R)) / R
        #       tau = omega/sqrt(omega^2 + eta/2)
        # if the spherical charge distribution is the product of above s-type
        # Gaussian with exponent eta and a very smooth function.
        # When R is large, the attenuated Coulomb integral is
        #   ~= (pi/eta)^3/2 erfc(tau*(eta/2)^0.5*R) / R
        #   ~= pi/(tau*eta^2*R^2) exp(-tau^2*eta*R^2/2)
        tau = self.omega / (self.omega**2 + eta/2)**.5
        rcut_sr = 10  # initial guess
        rcut_sr = (-np.log(direct_scf_tol * tau * (eta * rcut_sr)**2/np.pi) / (tau**2*eta/2))**.5
        logger.debug(self, 'eta = %g  rcut_sr = %g', eta, rcut_sr)

        # Ls is the translation vectors to mimic periodicity of a cell
        Ls = bvkcell.get_lattice_Ls(rcut=cell.rcut+rcut_sr)
        self.supmol_Ls = Ls = Ls[np.linalg.norm(Ls, axis=1).argsort()]
        nimgs = len(Ls)

        cell_dense, cell_sparse, ke_cutoff = _split_cell(cell, self.ke_cutoff)
        cell_merged = cell_dense + cell_sparse
        supmol = _make_extended_mole(cell_merged, Ls, Ks, direct_scf_tol)
        self.cell_dense = cell_dense
        self.cell_sparse = cell_sparse
        self.supmol = supmol

        bas_mask = supmol.bas_mask
        nkpts = len(kpts)
        bas_idx = np.append(cell_dense._bas_idx, cell_sparse._bas_idx)
        kbas_idx = bas_idx + np.arange(nkpts)[:,None] * cell.nbas
        bvkcell_shl_id = np.repeat(kbas_idx.reshape(1, -1), nimgs, axis=0)
        self.bvkcell_shl_id = bvkcell_shl_id.ravel()[bas_mask].astype(np.int32)

        smooth_mask = np.zeros((nkpts*nimgs, cell_merged.nbas), dtype=bool)
        smooth_mask[:,cell_dense.nbas:] = True
        smooth_mask = smooth_mask.ravel()[bas_mask]
        nbas_smooth = np.count_nonzero(smooth_mask)
        nbas_steep = supmol.nbas - nbas_smooth

        logger.info(self, 'sup-mol nbas = %d cGTO = %d pGTO = %d',
                    supmol.nbas, supmol.nao, supmol.npgto_nr())
        logger.debug(self, 'Steep basis in each cell nbas = %d cGTOs = %d pGTOs = %d',
                     cell_dense.nbas, cell_dense.nao, cell_dense.npgto_nr())
        logger.debug(self, 'Smooth basis in each cell nbas = %d cGTOs = %d pGTOs = %d',
                     cell_sparse.nbas, cell_sparse.nao, cell_sparse.npgto_nr())
        logger.debug(self, 'Steep basis in sup-mol %d, smooth basis in sup-mol %d',
                     nbas_steep, nbas_smooth)

        supmol.omega = -self.omega  # Set short range coulomb
        with supmol.with_integral_screen(direct_scf_tol**2):
            vhfopt = _vhf.VHFOpt(supmol, 'int2e_sph',
                                 qcondname=libpbc.PBCVHFsetnr_direct_scf)
        vhfopt.direct_scf_tol = direct_scf_tol
        self.vhfopt = vhfopt
        cpu1 = logger.timer(self, 'initializing vhfopt', *cpu0)

        supmol_only_s = supmol.copy()
        supmol_only_s._bas[:,gto.ANG_OF] = 0
        # Note: some basis has negative contraction coefficients.
        ovlp_mask = abs(supmol_only_s.intor_symmetric('int1e_ovlp')) > direct_scf_tol
        ovlp_mask = ovlp_mask.astype(np.int8)
        ovlp_mask[smooth_mask[:,None] & smooth_mask] = 2
        self.ovlp_mask = ovlp_mask

        self.lr_aft = lr_aft = _LongRangeAFT(cell, kpts, self.omega)
        lr_aft.ke_cutoff = self.ke_cutoff
        lr_aft.mesh = self.mesh

        # FIXME: if cell_sparse.nbas == 0
        if cell_sparse.nbas > 0:
            self.sr_aft = sr_aft = _ShortRangeAFT(cell_sparse, kpts, self.omega)
            sr_aft.ke_cutoff = self.ke_cutoff
            sr_aft.mesh = self.mesh
        return self

    def get_jk(self, dm_kpts, hermi=1, kpts=None, kpts_band=None,
               with_j=True, with_k=True, omega=None, exxdiv=None):
        if omega is not None:  # J/K for RSH functionals
            # TODO: call AFTDF.get_jk function
            raise NotImplementedError

        # Does not support to specify arbitrary kpts
        if kpts is not None and abs(kpts-self.kpts).max() > 1e-7:
            raise RuntimeError('kpts error')
        kpts = self.kpts

        if kpts_band is None:
            kpts_lst = kpts
        else:
            raise NotImplementedError

        cpu0 = (time.clock(), time.time())
        if self.supmol is None:
            self.build()

        cell = self.cell
        nkpts = kpts.shape[0]
        vhfopt = self.vhfopt
        supmol = self.supmol
        bvkcell = self.bvkcell
        phase = self.phase
        nao = cell.nao
        k_nao = bvkcell.nao

        sm_nbas = supmol.nbas
        if kpts_band is None:
            bvk_ao_loc = bvkcell.ao_loc
            bands_ao_loc = bvkcell.ao_loc[self.bvkcell_shl_id]
            nbands = nkpts

        if dm_kpts.ndim != 4:
            dm = dm_kpts.reshape(-1,nkpts,nao,nao)
        else:
            dm = dm_kpts
        n_dm = dm.shape[0]

        sc_dm = lib.einsum('Rk,nkuv,Sk->nRuSv', phase, dm, phase.conj())
        is_complex_dm = abs(sc_dm.imag).max() > 1e-6
        if is_complex_dm:
            sc_dm = np.vstack([sc_dm.real, sc_dm.imag])
        else:
            sc_dm = sc_dm.real
        sc_dm = np.asarray(sc_dm.reshape(-1, k_nao, k_nao), order='C')
        n_sc_dm = sc_dm.shape[0]

        # Cannot initialize dm_cond with vhfopt.set_dm(sc_dm, bvkcell._atm, bvkcell._bas, bvkcell._env)
        # because vhfopt.dm_cond requires shape == (sm_nbas, sm_nbas)
        dm_cond = [bvkcell.condense_to_shell(abs(d), np.max) for d in sc_dm]
        dm_cond = np.max(dm_cond, axis=0)
        libpbc.CVHFset_dm_cond(vhfopt._this,
                               dm_cond.ctypes.data_as(ctypes.c_void_p), dm_cond.size)
        dm_cond = None

        drv = libpbc.PBCVHF_direct_drv
        if with_j and with_k:
            fdot = libpbc.PBCVHF_contract_jk_s2kl
            vs = np.zeros((2, n_sc_dm, nao, nkpts, nao))
        elif with_j:
            fdot = libpbc.PBCVHF_contract_j_s2kl
            vs = np.zeros((1, n_sc_dm, nao, nkpts, nao))
        else:  # with_k
            fdot = libpbc.PBCVHF_contract_k_s2kl
            vs = np.zeros((1, n_sc_dm, nao, nkpts, nao))

        cell0_nbas = self.cell_dense.nbas + self.cell_sparse.nbas
        shls_slice = (0, cell0_nbas, 0, sm_nbas, 0, sm_nbas, 0, sm_nbas)
        drv(fdot, vs.ctypes.data_as(ctypes.c_void_p),
            sc_dm.ctypes.data_as(ctypes.c_void_p), ctypes.c_int(n_dm),
            ctypes.c_int(cell.nao), ctypes.c_int(nkpts), ctypes.c_int(nbands),
            ctypes.c_int(cell.nbas), (ctypes.c_int*8)(*shls_slice),
            bvk_ao_loc.ctypes.data_as(ctypes.c_void_p),
            self.bvkcell_shl_id.ctypes.data_as(ctypes.c_void_p),
            bands_ao_loc.ctypes.data_as(ctypes.c_void_p),
            self.ovlp_mask.ctypes.data_as(ctypes.c_void_p),
            vhfopt._cintopt, vhfopt._this,
            supmol._atm.ctypes.data_as(ctypes.c_void_p), ctypes.c_int(supmol.natm),
            supmol._bas.ctypes.data_as(ctypes.c_void_p), ctypes.c_int(supmol.nbas),
            supmol._env.ctypes.data_as(ctypes.c_void_p))

        if is_complex_dm:
            vs = vs[:,:n_dm] + vs[:,n_dm:] * 1j

        if with_j and with_k:
            vj, vk = vs
        elif with_j:
            vj, vk = vs[0], None
        else:
            vj, vk = None, vs[0]
        cpu1 = logger.timer(self, 'short range part vj and vk', *cpu0)

        if self.sr_aft is not None:
            sr_cell = self.sr_aft.cell
            bas_idx = sr_cell._bas_idx
            ao_loc = cell.ao_loc
            sr_ao_idx = np.hstack([np.arange(ao_loc[i], ao_loc[i+1]) for i in bas_idx])
            sr_dm = dm[:,:,sr_ao_idx[:,None],sr_ao_idx]
            vj2, vk2 = self.sr_aft.get_jk(sr_dm, hermi, kpts, kpts_band,
                                          with_j, with_k, exxdiv=exxdiv)
            cpu1 = logger.timer(self, 'smooth GTO vj and vk', *cpu1)

        vj1, vk1 = self.lr_aft.get_jk(dm_kpts, hermi, kpts, kpts_band,
                                      with_j, with_k, exxdiv=exxdiv)

        # expRk is almost the same to phase, except a normalization factor
        if kpts_band is None:
            expRk = np.exp(1j*np.dot(self.bvkmesh_Ls, kpts.T))
        if with_j:
            vj = lib.einsum('nuRv,Rk->nkuv', vj, expRk)
            if self.sr_aft is not None:
                vj[:,:,sr_ao_idx[:,None],sr_ao_idx] += vj2
            if dm_kpts.ndim == 3:  # KRHF
                vj = vj[0]
            vj += vj1

        if with_k:
            vk = lib.einsum('nuRv,Rk->nkuv', vk, expRk)
            if self.sr_aft is not None:
                vk[:,:,sr_ao_idx[:,None],sr_ao_idx] += vk2
            if dm_kpts.ndim == 3:  # KRHF
                vk = vk[0]
            vk += vk1

        cpu1 = logger.timer(self, 'long range part vj and vk', *cpu1)
        return vj, vk

class _LongRangeAFT(aft.AFTDF):
    def __init__(self, cell, kpts=np.zeros((1,3)), omega=None):
        self.omega = omega
        aft.AFTDF.__init__(self, cell, kpts)

    def weighted_coulG(self, kpt=np.zeros(3), exx=False, mesh=None):
        cell = self.cell
        if mesh is None:
            mesh = self.mesh
        if cell.omega != 0:
            raise RuntimeError('RangeSeparationJKBuilder cannot be used to evaluate '
                               'the long-range HF exchange in RSH functional')

        Gv, Gvbase, kws = cell.get_Gv_weights(mesh)
        coulG = pbctools.get_coulG(cell, kpt, exx, self, mesh, Gv,
                                   omega=self.omega)

        # Removing the G=0 contributions in the short-range exchange matrix.
        # This leads to exxdiv=None treatment. Without removing this term, it's
        # more or less vcut_ws exx correction.
        if not exx or exx == 'ewald':
            if gamma_point(kpt):
                gamma_idx = np.all((Gv == 0), axis=1)
                coulG[gamma_idx] -= np.pi/self.omega**2

        coulG *= kws
        return coulG

class _ShortRangeAFT(aft.AFTDF):
    def __init__(self, cell, kpts=np.zeros((1,3)), omega=None):
        self.omega = omega
        aft.AFTDF.__init__(self, cell, kpts)

    def weighted_coulG(self, kpt=np.zeros(3), exx=False, mesh=None):
        cell = self.cell
        if mesh is None:
            mesh = self.mesh
        if cell.omega != 0:
            raise RuntimeError('RangeSeparationJKBuilder cannot be used to evaluate '
                               'the long-range HF exchange in RSH functional')

        Gv, Gvbase, kws = cell.get_Gv_weights(mesh)
        coulG = pbctools.get_coulG(cell, kpt, None, self, mesh, Gv,
                                   omega=-self.omega)

        # Removing the G=0 contributions in the short-range exchange matrix.
        # This leads to exxdiv=None treatment. Without removing this term, it's
        # more or less vcut_ws exx correction.
        if not exx or exx == 'ewald':
            if gamma_point(kpt):
                gamma_idx = np.all((Gv == 0), axis=1)
                coulG[gamma_idx] += np.pi/self.omega**2

        coulG *= kws
        return coulG

def _make_extended_mole(cell, Ls, bvkmesh_Ls, precision=None):
    if precision is None:
        precision = cell.precision * 1e-2
    LKs = Ls[:,None,:] + bvkmesh_Ls
    nimgs, nk = LKs.shape[:2]

    nbas = cell.nbas
    bvkcell_shl_id = np.repeat(np.arange(nk*nbas).reshape(1,nk,nbas), nimgs, axis=0)

    supmol = cell.to_mol()
    supmol = pbctools.pbc._build_supcell_(supmol, cell, LKs.reshape(nimgs*nk, 3))

    supmol_only_s = supmol.copy()
    supmol_only_s._bas[:,gto.ANG_OF] = 0
    s0 = supmol_only_s.intor('int1e_ovlp', shls_slice=(0, nbas, 0, supmol.nbas))
    # Note: some basis has negative contraction coefficients
    s0max = abs(s0).max(axis=0)
    bas_mask = s0max > precision
    supmol._bas = supmol._bas[bas_mask]
    supmol.bvkcell_shl_id = bvkcell_shl_id.ravel()[bas_mask]
    supmol.bas_mask = bas_mask
    return supmol

def _split_cell(cell, ke_cut):
    from pyscf.gto import NPRIM_OF, NCTR_OF, PTR_EXP, PTR_COEFF, ATOM_OF
    from pyscf.pbc.dft.multigrid import _primitive_gto_cutoff
    log = logger.new_logger(cell)

    # Split shells based on rcut
    rcuts, kecuts = _primitive_gto_cutoff(cell, cell.precision)
    ao_loc = cell.ao_loc_nr()

    # cell that needs dense/sparse integration grids
    cell_dense = copy.copy(cell)
    cell_sparse = copy.copy(cell)

    _env = cell._env.copy()
    dense_bas = []
    sparse_bas = []
    # dense_bas_idx and sparse_bas_idx maps the shells in cell_dense and
    # cell_sparse to the original cell
    dense_bas_idx = []
    sparse_bas_idx = []
    dense_cell_rcut = 0
    ke_cutoff = 0
    for ib, orig_bas in enumerate(cell._bas):
        nprim = orig_bas[NPRIM_OF]
        nctr = orig_bas[NCTR_OF]
        ke = kecuts[ib]
        dense_mask = ke > ke_cut
        np1 = np.count_nonzero(dense_mask)
        if np1 == 0:
            sparse_bas.append(orig_bas)
            sparse_bas_idx.append(ib)
            log.debug1('bas %d, %d smooth functions', ib, nprim)

        elif np1 == nprim:
            dense_bas.append(orig_bas)
            dense_bas_idx.append(ib)
            ke_cutoff = max(ke_cutoff, ke.max())
            dense_cell_rcut = max(dense_cell_rcut, rcuts[ib].max())
            log.debug1('bas %d, %d steep functions', ib, nprim)

        else:
            es = cell.bas_exp(ib)
            cs = cell._libcint_ctr_coeff(ib)
            e1 = es[dense_mask]
            e2 = es[~dense_mask]
            c1 = cs[dense_mask]
            c2 = cs[~dense_mask]
            pexp = orig_bas[PTR_EXP]
            pcoeff = orig_bas[PTR_COEFF]
            _env[pcoeff:pcoeff+nprim*nctr] = np.append(c1.T.ravel(), c2.T.ravel())
            _env[pexp:pexp+nprim] = np.append(e1, e2)

            bas1 = orig_bas.copy()
            bas2 = orig_bas.copy()
            bas1[NPRIM_OF] = np1
            bas2[NPRIM_OF] = nprim - np1
            bas1[PTR_EXP] = pexp
            bas2[PTR_EXP] = pexp + e1.size
            bas1[PTR_COEFF] = pcoeff
            bas2[PTR_COEFF] = pcoeff + c1.size

            dense_bas.append(bas1)
            dense_bas_idx.append(ib)
            sparse_bas.append(bas2)
            sparse_bas_idx.append(ib)
            log.debug1('bas %d, %d steep functions, %d smooth functions',
                       ib, np1, nprim - np1)

            ke_cutoff = max(ke_cutoff, ke[dense_mask].max())
            dense_cell_rcut = max(dense_cell_rcut, rcuts[ib][dense_mask].max())
    log.debug1('rcut for steep functions %g', dense_cell_rcut)
    log.debug1('rcut for smooth functions %g', cell_sparse.rcut)
    log.debug1('ke_cutoff for steep functions %g', ke_cutoff)

    cell_dense._bas = np.asarray(dense_bas, dtype=np.int32, order='C').reshape(-1, gto.BAS_SLOTS)
    cell_dense.rcut = dense_cell_rcut
    cell_dense._bas_idx = np.asarray(dense_bas_idx, dtype=np.int32)
    cell_sparse._bas = np.asarray(sparse_bas, dtype=np.int32, order='C').reshape(-1, gto.BAS_SLOTS)
    cell_sparse._bas_idx = np.asarray(sparse_bas_idx, dtype=np.int32)
    return cell_dense, cell_sparse, ke_cutoff

def _fpointer(name):
    import _ctypes
    return ctypes.c_void_p(_ctypes.dlsym(libpbc._handle, name))

if __name__ == '__main__':
    from pyscf.pbc.gto import Cell
    cells = []

    cell = Cell()
    cell.a = np.eye(3)*1.8
    cell.atom = '''He     0.      0.      0.
                   He     0.4917  0.4917  0.4917
                '''
    cell.basis = {'He': [[0, [2.1, 1],
                             [0.1, .2]
                         ],
                         [1, [0.3, 1]],
                         #[1, [1.5, 1]],
                        ]}
    cell.build()
    cell.verbose = 6
    cells.append(cell)

    if 1:
        cell = Cell().build(a = '''
3.370137329, 0.000000000, 0.000000000
0.000000000, 3.370137329, 0.000000000
0.000000000, 0.000000000, 3.370137329
                            ''',
                            unit = 'B',
                            atom = '''
C 0.000000000000   0.000000000000   0.000000000000
C 1.685068664391   1.685068664391   1.685068664391''',
                            basis='''
C S
4.3362376436      0.1490797872
1.2881838513      -0.0292640031
0.4037767149      -0.688204051
0.1187877657      -0.3964426906
C P
4.3362376436      -0.0878123619
1.2881838513      -0.27755603
0.4037767149      -0.4712295093
0.1187877657      -0.4058039291
''')
        cell.verbose = 6
        cells.append(cell)

    for cell in cells:
        kpts = cell.make_kpts([1,1,1])
        mf = cell.KRHF(kpts=kpts)#.run()
        #dm = mf.make_rdm1()
        np.random.seed(1)
        dm = np.random.rand(len(kpts), cell.nao, cell.nao)
        dm = dm + dm.transpose(0,2,1)
        jref, kref = mf.get_jk(cell, dm, kpts=kpts)
        ej = np.einsum('kij,kji->', jref, dm)
        ek = np.einsum('kij,kji->', kref, dm) * .5

        #print(_estimate_omega(cell))

        jk_builder = RangeSeparationJKBuilder(cell, kpts)
        #jk_builder.build(omega=0.8)
        #jk_builder.mesh = [6,6,6]
        #print(jk_builder.omega, jk_builder.mesh)
        vj, vk = jk_builder.get_jk(dm, kpts=kpts, exxdiv=mf.exxdiv)
        print(abs(vj - jref).max())
        print(abs(vk - kref).max())
        print('ej_ref', ej, 'ek_ref', ek)
        print('ej', np.einsum('kij,kji->', vj, dm).real,
              'ek', np.einsum('kij,kji->', vk, dm).real * .5)