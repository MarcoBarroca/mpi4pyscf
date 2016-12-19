#!/usr/bin/env python
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

'''
Exact density fitting with Gaussian and planewaves
Ref:
'''

import time
import platform
import copy
import ctypes
import numpy
import scipy.linalg
import h5py

from pyscf import lib
from pyscf.pbc import gto
from pyscf.pbc import tools
from pyscf.pbc.df import incore
from pyscf.pbc.df import outcore
from pyscf.pbc.df import ft_ao
from pyscf.pbc.df import mdf
#from pyscf.pbc.df.df import fuse_auxcell_, make_modrho_basis, \
#        make_modchg_basis, estimate_eta, unique
from pyscf.pbc.df.df import fuse_auxcell, make_modrho_basis, \
        estimate_eta, unique
from pyscf.pbc.df.df_jk import zdotNN, zdotCN, zdotNC
from pyscf.gto.mole import ANG_OF, PTR_COORD
from pyscf.ao2mo.outcore import balance_segs

from mpi4pyscf.lib import logger
from mpi4pyscf.tools import mpi
from mpi4pyscf.pbc.df import mdf_jk
from mpi4pyscf.pbc.df import mdf_ao2mo
from mpi4pyscf.pbc.df import df

comm = mpi.comm
rank = mpi.rank


def _build(mydf, j_only=False, with_j3c=True):
# Unlike DF and PWDF class, here MDF objects are synced once
    if mpi.pool.size == 1:
        return mdf.MDF.build(mydf, j_only, with_j3c)

    mydf = _sync_mydf(mydf)
    cell = mydf.cell
    log = logger.Logger(mydf.stdout, mydf.verbose)
    info = rank, platform.node(), platform.os.getpid()
    log.debug('MPI info (rank, host, pid)  %s', comm.gather(info))

    t1 = (time.clock(), time.time())
    if mydf.eta is None:
        mydf.eta = estimate_eta(cell)
        log.debug('Set smooth gaussian eta to %.9g', mydf.eta)
    mydf.dump_flags()

    mydf.auxcell = make_modrho_basis(cell, mydf.auxbasis, mydf.eta)

    mydf._j_only = j_only
    if j_only:
        kptij_lst = numpy.hstack((mydf.kpts,mydf.kpts)).reshape(-1,2,3)
    else:
        kptij_lst = [(ki, mydf.kpts[j])
                     for i, ki in enumerate(mydf.kpts) for j in range(i+1)]
        kptij_lst = numpy.asarray(kptij_lst)

    if not isinstance(mydf._cderi, str):
        if isinstance(mydf._cderi_file, str):
            mydf._cderi = mydf._cderi_file
        else:
            mydf._cderi = mydf._cderi_file.name

    if with_j3c:
        if mydf.approx_sr_level != 0:
            raise NotImplementedError

        _make_j3c(mydf, cell, mydf.auxcell, kptij_lst)
        t1 = log.timer_debug1('_make_j3c', *t1)
    else:
        raise NotImplementedError
    return mydf
build = mpi.parallel_call(_build)


class _MDF(mdf.MDF, df._DF):

    build = build
    _build = _build
    get_nuc = df.get_nuc

    def pack(self):
        return {'verbose'   : self.verbose,
                'max_memory': self.max_memory,
                'kpts'      : self.kpts,
                'gs'        : self.gs,
                'eta'       : self.eta,
                'exxdiv'    : self.exxdiv,
                'blockdim'  : self.blockdim,
                'auxbasis'  : self.auxbasis,
                'metric'    : self.metric,
                'approx_sr_level' : self.approx_sr_level}
    def unpack_(self, dfdic):
        self.__dict__.update(dfdic)
        return self

    def get_jk(self, dm, hermi=1, kpts=None, kpt_band=None,
               with_j=True, with_k=True, exxdiv='ewald'):
        if kpts is None:
            if numpy.all(self.kpts == 0):
                # Gamma-point calculation by default
                kpts = numpy.zeros(3)
            else:
                kpts = self.kpts
        else:
            kpts = numpy.asarray(kpts)

        if kpts.shape == (3,):
            return mdf_jk.get_jk(self, dm, hermi, kpts, kpt_band, with_j,
                                 with_k, exxdiv)

        vj = vk = None
        if with_k:
            vk = mdf_jk.get_k_kpts(self, dm, hermi, kpts, kpt_band, exxdiv)
        if with_j:
            vj = mdf_jk.get_j_kpts(self, dm, hermi, kpts, kpt_band)
        return vj, vk

    get_eri = get_ao_eri = mdf_ao2mo.get_eri
    ao2mo = get_mo_eri = mdf_ao2mo.general

MDF = mpi.register_class(_MDF)


def _make_j3c(mydf, cell, auxcell, kptij_lst):
    log = logger.Logger(mydf.stdout, mydf.verbose)
    t1 = t0 = (time.clock(), time.time())
    fused_cell, fuse = fuse_auxcell(mydf, mydf.auxcell)
    ao_loc = cell.ao_loc_nr()
    nao = ao_loc[-1]
    naux = auxcell.nao_nr()
    nkptij = len(kptij_lst)

    gs = mydf.gs
    Gv, Gvbase, kws = cell.get_Gv_weights(gs)
    b = cell.reciprocal_vectors()
    gxyz = lib.cartesian_prod([numpy.arange(len(x)) for x in Gvbase])
    ngs = gxyz.shape[0]

    kptis = kptij_lst[:,0]
    kptjs = kptij_lst[:,1]
    kpt_ji = kptjs - kptis
    uniq_kpts, uniq_index, uniq_inverse = unique(kpt_ji)
    # j2c ~ (-kpt_ji | kpt_ji)
    j2c = fused_cell.pbc_intor('cint2c2e_sph', hermi=1, kpts=uniq_kpts)
    t1 = log.timer_debug1('2c2e', *t1)
    kLRs = []
    kLIs = []
    for k, kpt in enumerate(uniq_kpts):
        aoaux = ft_ao.ft_ao(fused_cell, Gv, None, b, gxyz, Gvbase, kpt).T
        aoaux = fuse(aoaux)
        coulG = numpy.sqrt(mydf.weighted_coulG(kpt, False, gs))
        kLR = (aoaux.real * coulG).T
        kLI = (aoaux.imag * coulG).T
        if not kLR.flags.c_contiguous: kLR = lib.transpose(kLR.T)
        if not kLI.flags.c_contiguous: kLI = lib.transpose(kLI.T)
        aoaux = None

        j2c[k] = fuse(fuse(j2c[k]).T).T.copy()
        for p0, p1 in mydf.mpi_prange(0, ngs):
            if is_zero(kpt):  # kpti == kptj
                j2cR = lib.dot(kLR[p0:p1].T, kLR[p0:p1])
                j2cR = lib.dot(kLI[p0:p1].T, kLI[p0:p1], 1, j2cR, 1)
                j2c[k] -= mpi.allreduce(j2cR)
            else:
                 # aoaux ~ kpt_ij, aoaux.conj() ~ kpt_kl
                j2cR, j2cI = zdotCN(kLR[p0:p1].T, kLI[p0:p1].T, kLR[p0:p1], kLI[p0:p1])
                j2cR = mpi.allreduce(j2cR)
                j2cI = mpi.allreduce(j2cI)
                j2c[k] -= j2cR + j2cI * 1j

        kLR *= coulG.reshape(-1,1)
        kLI *= coulG.reshape(-1,1)
        kLRs.append(kLR)
        kLIs.append(kLI)
        aoaux = kLR = kLI = j2cR = j2cI = coulG = None

    aosym_s2 = numpy.einsum('ix->i', abs(kptis-kptjs)) < 1e-9
    j_only = numpy.all(aosym_s2)
    vbar = mydf.auxbar(fused_cell)
    vbar = fuse(vbar)
    ovlp = cell.pbc_intor('cint1e_ovlp_sph', hermi=1, kpts=kptjs[aosym_s2])

    if mydf.metric.upper() == 'S':
        s_aux = auxcell.pbc_intor('cint1e_ovlp_sph', hermi=1, kpts=uniq_kpts)
    else:
        s_aux = auxcell.pbc_intor('cint1e_kin_sph', hermi=1, kpts=uniq_kpts)
        s_aux = [x*2 for x in s_aux]
    s_aux = [scipy.linalg.cho_factor(x) for x in s_aux]
    t1 = log.timer_debug1('aoaux and int2c', *t1)

# Estimates the buffer size based on the last contraction in G-space.
# This contraction requires to hold nkptj copies of (naux,?) array
# simultaneously in memory.
    mem_now = max(comm.allgather(lib.current_memory()[0]))
    max_memory = max(2000, mydf.max_memory - mem_now)
    #nkptj_max = max(numpy.unique(uniq_inverse, return_counts=True)[1])
    nkptj_max = max((uniq_inverse==x).sum() for x in set(uniq_inverse))
    #buflen = max(int(min(numpy.sqrt(max_memory*.5e6/16/naux/(nkptj_max+2)),
    #                     nao/3/numpy.sqrt(mpi.pool.size))), 1)
    #chunks = (buflen, buflen)
    buflen = max(int(min(max_memory*.5e6/16/naux/(nkptj_max+2)/nao,
                         nao/3/mpi.pool.size)), 1)
    chunks = (buflen, nao)

    j3c_jobs = grids2d_int3c_jobs(cell, auxcell, kptij_lst, chunks)
    log.debug1('max_memory = %d MB (%d in use)  chunks %s',
               max_memory, mem_now, chunks)
    log.debug2('j3c_jobs %s', j3c_jobs)
    if mydf.metric.upper() == 'S':
        Lpq_intor = 'cint3c1e_sph'
    else:
        Lpq_intor = 'cint3c1e_p2_sph'

    if h5py.is_hdf5(mydf._cderi):
        feri = h5py.File(mydf._cderi)
    else:
        feri = h5py.File(mydf._cderi, 'w')

    def gen_int3c(auxcell, intor, label, job_id, ish0, ish1):
        aux_loc = auxcell.ao_loc_nr('ssc' in intor)
        naux = aux_loc[-1]
        dataname = '%s/%d' % (label, job_id)
        if dataname in feri:
            del(feri[dataname])

        xyz = numpy.asarray(cell.atom_coords(), order='C')
        ptr_coordL = cell._atm[:,PTR_COORD]
        ptr_coordL = numpy.vstack((ptr_coordL,ptr_coordL+1,ptr_coordL+2)).T.copy('C')
        Ls = cell.get_lattice_Ls(cell.nimgs)

        di = ao_loc[ish1] - ao_loc[ish0]
        dij = di * nao
        buflen = max(8, int(max_memory*1e6/16/(nkptij*dij)))
        auxranges = balance_segs(aux_loc[1:]-aux_loc[:-1], buflen)
        buflen = max([x[2] for x in auxranges])
        buf = [numpy.zeros(dij*buflen, dtype=numpy.complex128) for k in range(nkptij)]

        ints = incore._wrap_int3c(cell, auxcell, intor, 1, Ls, buf)
        atm, bas, env = ints._envs[:3]

        for kpt_id, kptij in enumerate(kptij_lst):
            key = '%s/%d' % (dataname, kpt_id)
            shape = (naux, dij)
            if gamma_point(kptij):
                feri.create_dataset(key, shape, 'f8')
            else:
                feri.create_dataset(key, shape, 'c16')

        naux0 = 0
        for istep, auxrange in enumerate(auxranges):
            log.alldebug2("aux_e2 %s job_id %d step %d", label, job_id, istep)
            sh0, sh1, nrow = auxrange
            c_shls_slice = (ctypes.c_int*6)(ish0, ish1, cell.nbas, cell.nbas*2,
                                            cell.nbas*2+sh0, cell.nbas*2+sh1)
            if j_only:
                for l, L1 in enumerate(Ls):
                    env[ptr_coordL] = xyz + L1
                    e = numpy.dot(Ls[:l+1]-L1, kptjs.T)  # Lattice sum over half of the images {1..l}
                    exp_Lk = numpy.exp(1j * numpy.asarray(e, order='C'))
                    exp_Lk[l] = .5
                    ints(exp_Lk, c_shls_slice)
            else:
                for l, L1 in enumerate(Ls):
                    env[ptr_coordL] = xyz + L1
                    e = numpy.dot(Ls, kptjs.T) - numpy.dot(L1, kptis.T)
                    exp_Lk = numpy.exp(1j * numpy.asarray(e, order='C'))
                    ints(exp_Lk, c_shls_slice)

            for k, kptij in enumerate(kptij_lst):
                h5dat = feri['%s/%d'%(dataname,k)]
                mat = numpy.ndarray((di,nao,nrow), order='F',
                                    dtype=numpy.complex128, buffer=buf[k])
                mat = mat.transpose(2,0,1)
                if gamma_point(kptij):
                    mat = mat.real
                h5dat[naux0:naux0+nrow] = mat.reshape(nrow,-1)
                mat[:] = 0
            naux0 += nrow

    def fuse_j3c(Lpq, j3c, uniq_k_id, key, j3cR, j3cI):
        Lpq = scipy.linalg.cho_solve(s_aux[uniq_k_id], Lpq)
        feri['Lpq'+key][:] = Lpq
        j3c = fuse(j3c)
        j3c = lib.dot(j2c[uniq_k_id], Lpq, -.5, j3c, 1)
        j3cR.append(numpy.asarray(j3c.real, order='C'))
        if j3c.dtype == numpy.complex128:
            j3cI.append(numpy.asarray(j3c.imag, order='C'))
        else:
            j3cI.append(None)

    if j_only:
        ccsum_fac = .5
    else:
        ccsum_fac = 1
    def ft_fuse(job_id, uniq_kptji_id, sh0, sh1):
        kpt = uniq_kpts[uniq_kptji_id]  # kpt = kptj - kpti
        adapted_ji_idx = numpy.where(uniq_inverse == uniq_kptji_id)[0]
        adapted_kptjs = kptjs[adapted_ji_idx]
        nkptj = len(adapted_kptjs)
        kLR = kLRs[uniq_kptji_id]
        kLI = kLIs[uniq_kptji_id]

        write_handler = None
        j3cR = []
        j3cI = []
        i0 = ao_loc[sh0]
        i1 = ao_loc[sh1]
        for k, idx in enumerate(adapted_ji_idx):
            key = '-chunks/%d/%d' % (job_id, idx)
            Lpq = numpy.asarray(feri['Lpq'+key])
            j3c = numpy.asarray(feri['j3c'+key])
            if is_zero(kpt):
                for i, c in enumerate(vbar):
                    if c != 0:
                        j3c[i] -= c*ccsum_fac * ovlp[k][i0:i1].ravel()
            write_handler = async_write(write_handler, fuse_j3c, Lpq, j3c,
                                        uniq_kptji_id, key, j3cR, j3cI)
            Lpq = j3c = None
        if write_handler is not None:
            write_handler.join()
        write_handler = None

        ncol = j3cR[0].shape[1]
        Gblksize = max(16, int(max_memory*1e6/16/ncol/(nkptj+1)))  # +1 for pqkRbuf/pqkIbuf
        Gblksize = min(Gblksize, ngs, 16384)
        pqkRbuf = numpy.empty(ncol*Gblksize)
        pqkIbuf = numpy.empty(ncol*Gblksize)
        # buf for ft_aopair
        buf = numpy.zeros((nkptj,ncol*Gblksize), dtype=numpy.complex128)
        log.alldebug2('    blksize (%d,%d)', Gblksize, ncol)

        shls_slice = (sh0, sh1, 0, cell.nbas)
        ni = ncol // nao
        for p0, p1 in lib.prange(0, ngs, Gblksize):
            ft_ao._ft_aopair_kpts(cell, Gv[p0:p1], shls_slice, 's1', b,
                                  gxyz[p0:p1], Gvbase, kpt, adapted_kptjs, out=buf)
            nG = p1 - p0
            for k, ji in enumerate(adapted_ji_idx):
                aoao = numpy.ndarray((nG,ni,nao), dtype=numpy.complex128,
                                     order='F', buffer=buf[k])
                pqkR = numpy.ndarray((ni,nao,nG), buffer=pqkRbuf)
                pqkI = numpy.ndarray((ni,nao,nG), buffer=pqkIbuf)
                pqkR[:] = aoao.real.transpose(1,2,0)
                pqkI[:] = aoao.imag.transpose(1,2,0)
                aoao[:] = 0
                pqkR = pqkR.reshape(-1,nG)
                pqkI = pqkI.reshape(-1,nG)

                if is_zero(kpt):  # kpti == kptj
                    # *.5 for hermi_sum at the assemble step
                    if gamma_point(adapted_kptjs[k]):
                        lib.dot(kLR[p0:p1].T, pqkR.T, -ccsum_fac, j3cR[k], 1)
                        lib.dot(kLI[p0:p1].T, pqkI.T, -ccsum_fac, j3cR[k], 1)
                    else:
                        zdotCN(kLR[p0:p1].T, kLI[p0:p1].T, pqkR.T, pqkI.T,
                               -ccsum_fac, j3cR[k], j3cI[k], 1)
                else:
                    zdotCN(kLR[p0:p1].T, kLI[p0:p1].T, pqkR.T, pqkI.T,
                           -1, j3cR[k], j3cI[k], 1)

        for k, idx in enumerate(adapted_ji_idx):
            if is_zero(kpt) and gamma_point(adapted_kptjs[k]):
                feri['j3c-chunks/%d/%d'%(job_id,idx)][:naux] = j3cR[k]
            else:
                feri['j3c-chunks/%d/%d'%(job_id,idx)][:naux] = j3cR[k] + j3cI[k]*1j

    t2 = t1
    j3c_workers = numpy.zeros(len(j3c_jobs), dtype=int)
    #for job_id, ish0, ish1 in mpi.work_share_partition(j3c_jobs):
    for job_id, ish0, ish1 in mpi.work_stealing_partition(j3c_jobs):
        gen_int3c(auxcell, Lpq_intor, 'Lpq-chunks', job_id, ish0, ish1)
        t2 = log.alltimer_debug2('int Lpq %d' % job_id, *t2)
        gen_int3c(fused_cell, 'cint3c2e_sph', 'j3c-chunks', job_id, ish0, ish1)
        t2 = log.alltimer_debug2('int j3c %d' % job_id, *t2)

        for k, kpt in enumerate(uniq_kpts):
            ft_fuse(job_id, k, ish0, ish1)
            t2 = log.alltimer_debug2('ft-fuse %d k %d' % (job_id, k), *t2)

        j3c_workers[job_id] = rank
    j3c_workers = mpi.allreduce(j3c_workers)
    log.debug2('j3c_workers %s', j3c_workers)
    s_aux = j2c = kLRs = kLIs = ovlp = vbar = fuse = fuse_j3c = gen_int3c = ft_fuse = None
    t1 = log.timer_debug1('int3c and fuse', *t1)

    if 'Lpq' in feri: del(feri['Lpq'])
    if 'j3c' in feri: del(feri['j3c'])
    segsize = (naux+mpi.pool.size-1) // mpi.pool.size
    naux0 = min(naux, rank*segsize)
    naux1 = min(naux, rank*segsize+segsize)
    nrow = naux1 - naux0
    for k, kptij in enumerate(kptij_lst):
        if gamma_point(kptij):
            dtype = 'f8'
        else:
            dtype = 'c16'
        if aosym_s2[k]:
            nao_pair = nao * (nao+1) // 2
        else:
            nao_pair = nao * nao
        feri.create_dataset('Lpq/%d'%k, (nrow,nao_pair), dtype, maxshape=(None,nao_pair))
        feri.create_dataset('j3c/%d'%k, (nrow,nao_pair), dtype, maxshape=(None,nao_pair))

    dims = numpy.asarray([ao_loc[i1]-ao_loc[i0] for x,i0,i1 in j3c_jobs])
    dims = numpy.hstack([dims[j3c_workers==w] * nao for w in range(mpi.pool.size)])
    job_idx = numpy.hstack([numpy.where(j3c_workers==w)[0]
                            for w in range(mpi.pool.size)])
    segs_loc = numpy.append(0, numpy.cumsum(dims))
    segs_loc = [(segs_loc[j], segs_loc[j+1]) for j in numpy.argsort(job_idx)]
    def load(label, k, p0, p1):
        slices = [(min(i*segsize+p0,naux), min(i*segsize+p1, naux))
                  for i in range(mpi.pool.size)]
        segs = []
        for p0, p1 in slices:
            val = []
            for job_id, worker in enumerate(j3c_workers):
                if rank == worker:
                    key = '-chunks/%d/%d' % (job_id, k)
                    val.append(feri[label+key][p0:p1].ravel())
            if val:
                segs.append(numpy.hstack(val))
            else:
                segs.append(numpy.zeros(0))
        return segs

    def save(label, k, p0, p1, segs):
        segs = mpi.alltoall(segs)
        loc0, loc1 = min(p0, naux-naux0), min(p1, naux-naux0)
        nL = loc1 - loc0
        if nL > 0:
            segs = [segs[i0*nL:i1*nL].reshape(nL,-1) for i0,i1 in segs_loc]
            segs = numpy.hstack(segs)
            if j_only:
                segs = lib.hermi_sum(segs.reshape(-1,nao,nao), axes=(0,2,1))
            if aosym_s2[k]:
                segs = lib.pack_tril(segs.reshape(-1,nao,nao))
            feri['%s/%d'%(label,k)][loc0:loc1] = segs

    mem_now = max(comm.allgather(lib.current_memory()[0]))
    max_memory = max(2000, min(8000, mydf.max_memory - mem_now))
    if numpy.all(aosym_s2):
        if gamma_point(kptij_lst):
            blksize = max(16, int(max_memory*.5e6/8/nao**2))
        else:
            blksize = max(16, int(max_memory*.5e6/16/nao**2))
    else:
        blksize = max(16, int(max_memory*.5e6/16/nao**2/2))
    log.debug1('max_momory %d MB (%d in use), blksize %d',
               max_memory, mem_now, blksize)

    t2 = t1
    write_handler = None
    for k, kptji in enumerate(kptij_lst):
        for p0, p1 in lib.prange(0, segsize, blksize):
            segs = load('j3c', k, p0, p1)
            write_handler = async_write(write_handler, save, 'j3c', k, p0, p1, segs)
            segs = None
            segs = load('Lpq', k, p0, p1)
            write_handler = async_write(write_handler, save, 'Lpq', k, p0, p1, segs)
            segs = None
            t2 = log.timer_debug1('assemble k=%d %d:%d (in %d)' % (k, p0, p1, nrow), *t2)
    if write_handler is not None:
        write_handler.join()
    write_handler = None

    if 'Lpq-chunks' in feri: del(feri['Lpq-chunks'])
    if 'j3c-chunks' in feri: del(feri['j3c-chunks'])
    t1 = log.alltimer_debug1('assembling Lpq j3c', *t1)

    if 'Lpq-kptij' in feri: del(feri['Lpq-kptij'])
    if 'j3c-kptij' in feri: del(feri['j3c-kptij'])
    feri['Lpq-kptij'] = kptij_lst
    feri['j3c-kptij'] = kptij_lst
    feri.close()

def grids2d_int3c_jobs(cell, auxcell, kptij_lst, chunks):
    ao_loc = cell.ao_loc_nr()
    nao = ao_loc[-1]
    segs = ao_loc[1:]-ao_loc[:-1]
    ij_ranges = balance_segs(segs, chunks[0])

    jobs = [(job_id, i0, i1) for job_id, (i0, i1, x) in enumerate(ij_ranges)]
    return jobs

def is_zero(kpt):
    return abs(kpt).sum() < mdf.KPT_DIFF_TOL
gamma_point = is_zero

def _sync_mydf(mydf):
    return mydf.unpack_(comm.bcast(mydf.pack()))

def async_write(thread_io, fn, *args):
    if thread_io is not None:
        thread_io.join()
    thread_io = lib.background_thread(fn, *args)
    return thread_io


if __name__ == '__main__':
    from pyscf.pbc import gto as pgto
    from mpi4pyscf.pbc import df
    cell = pgto.M(atom='He 0 0 0; He 0 0 1', h=numpy.eye(3)*4, gs=[5]*3)
    mydf = df.MDF(cell, kpts)

    v = mydf.get_nuc()
    print(v.shape)
    #v = mydf.get_pp(kpts)
    #print(v.shape)

    nao = cell.nao_nr()
    dm = numpy.ones((nao,nao))
    vj, vk = mydf.get_jk(dm, kpts=kpts[0])
    print(vj.shape)
    print(vk.shape)

    dm_kpts = [dm]*5
    vj, vk = mydf.get_jk(dm_kpts, kpts=kpts)
    print(vj.shape)
    print(vk.shape)

    mydf.close()

