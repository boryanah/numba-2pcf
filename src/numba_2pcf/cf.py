'''
A simple 2PCF, using a particle grid and N^2
'''

import numpy as np
import numba as nb
import astropy.table
from astropy.table import Table

from . import particle_grid

__all__ = ['numba_2pcf', 'jackknife']

_fastmath = True
_parallel = True

@nb.njit(fastmath=_fastmath)
def _1d_to_3d(i,ngrid):
    '''i is flat index, n1d is grid size'''
    
    X = np.empty(3,dtype=np.int64)
    X[0] = i // (ngrid[1]*ngrid[2])
    X[1] = i // ngrid[2] - X[0]*ngrid[2]
    X[2] = i % ngrid[2]
    
    return X

@nb.njit(fastmath=_fastmath)
def _do_cell_pair(pos1, pos2, Rmax, nbin, Xoff, counts):
    dtype = pos1.dtype
    inv_bw = dtype.type(nbin/Rmax)
    Rmax2 = Rmax*Rmax
    N1,N2 = len(pos1), len(pos2)
    for i in range(N1):
        p1 = pos1[i]
        for j in range(N2):
            p2 = pos2[j]
            # Early exit conditions
            # TODO: could exploit cell sorting better
            zdiff = np.abs(p1[2] - p2[2] + Xoff[2])
            if zdiff > Rmax:
                continue
            ydiff = np.abs(p1[1] - p2[1] + Xoff[1])
            if ydiff > Rmax:
                continue
            xdiff = np.abs(p1[0] - p2[0] + Xoff[0])
            if xdiff > Rmax:
                continue
            
            r2 = xdiff**2 + ydiff**2 + zdiff**2
            if r2 > Rmax2:
                continue
            r = np.sqrt(r2)
            b = int(r*inv_bw)
            counts[b] += 1

@nb.njit(fastmath=_fastmath) # what does fast math do? B.H.
def _do_cell_pairwise_vel(pos1, pos2, vel1, vel2, Rmax, nbin, Xoff, counts, weight_counts, norm_counts):
    dtype = pos1.dtype
    inv_bw = dtype.type(nbin/Rmax) # is this assuming linear bins starting at zero? B.H.
    Rmax2 = Rmax*Rmax
    two = dtype.type(2.)
    zero = dtype.type(0.)
    N1,N2 = len(pos1), len(pos2)
    for i in range(N1):
        p1 = pos1[i]
        for j in range(N2):
            p2 = pos2[j]
            # Early exit conditions
            # TODO: could exploit cell sorting better
            zdiff = (p1[2] - p2[2] + Xoff[2])
            if np.abs(zdiff) > Rmax:
                continue
            ydiff = (p1[1] - p2[1] + Xoff[1])
            if np.abs(ydiff) > Rmax:
                continue
            xdiff = (p1[0] - p2[0] + Xoff[0])
            if np.abs(xdiff) > Rmax:
                continue
            
            r2 = xdiff**2 + ydiff**2 + zdiff**2
            if r2 > Rmax2:
                continue
            r = np.sqrt(r2)
            
            b = int(r*inv_bw)
            counts[b] += 1

            if r > zero:
                p12 = two * (zdiff/r)
                v1 = vel1[i][2]
                v2 = vel2[j][2]
                weight_counts[b] += two * (v1-v2) * p12 # B.H. only z component
                norm_counts[b] += p12**two

@nb.njit(parallel=_parallel,fastmath=_fastmath)
def _2pcf(psort, offsets, ngrid, box, Rmax, nbin):
    dtype = psort.dtype
    
    ncell = np.prod(ngrid)
    s = offsets  # length ngrid^3 + 1
    
    nw = np.array([3,3,3])  # neighbor width
    nneigh = np.prod(nw)
    
    nthread = nb.get_num_threads()
    thread_counts = np.zeros((nthread,nbin), dtype=np.int64)
    
    # loop over cell pairs
    for cpair in nb.prange(ncell*nneigh):
        t = nb.np.ufunc.parallel._get_thread_id()

        c = cpair // nneigh  # 1d primary cell index
        off1d = cpair % nneigh  # 0..26

        nprimary = s[c+1] - s[c]
        if nprimary == 0:  # optimize the case of sparse cells
            continue

        # global neighbor index
        c3d = _1d_to_3d(c,ngrid)
        off3d = _1d_to_3d(off1d,nw)
        d3d = c3d + off3d - 1

        # periodic neighbor index wrap
        Xoff = np.zeros(3, dtype=dtype)
        for j in range(3):
            if d3d[j] >= ngrid[j]:
                d3d[j] -= ngrid[j]
                Xoff[j] -= box
            if d3d[j] < 0:
                d3d[j] += ngrid[j]
                Xoff[j] += box
        # 1d neighbor index
        d = d3d[0]*ngrid[1]*ngrid[2] + d3d[1]*ngrid[2] + d3d[2]

        nsecondary = s[d+1] - s[d]
        if nsecondary == 0:
            continue
        
        _do_cell_pair(psort[s[c]:s[c+1]],
                      psort[s[d]:s[d+1]],
                      Rmax, nbin, Xoff,
                      thread_counts[t],
        )
    
    counts = thread_counts.sum(axis=0)
    
    # no self-counts
    counts[0] -= len(psort)
    
    return counts


@nb.njit(parallel=_parallel,fastmath=_fastmath)
def _pairwise(psort, vsort, offsets, ngrid, box, Rmax, nbin, periodic):
    dtype = psort.dtype
    
    ncell = np.prod(ngrid)
    s = offsets  # length ngrid^3 + 1
    
    nw = np.array([3,3,3])  # neighbor width
    nneigh = np.prod(nw)
    
    nthread = nb.get_num_threads()
    thread_counts = np.zeros((nthread,nbin), dtype=np.int64)

    zero = dtype.type(0.)
    thread_weight_counts = np.zeros((nthread,nbin), dtype=dtype)
    thread_norm_counts = np.zeros((nthread,nbin), dtype=dtype)
    
    # loop over cell pairs
    for cpair in nb.prange(ncell*nneigh):
        # flag for skipping pair if it requires wrapping (only if periodic=False)
        skip = False
        
        t = nb.np.ufunc.parallel._get_thread_id()

        c = cpair // nneigh  # 1d primary cell index
        off1d = cpair % nneigh  # 0..26

        nprimary = s[c+1] - s[c]
        if nprimary == 0:  # optimize the case of sparse cells
            continue

        # global neighbor index
        c3d = _1d_to_3d(c,ngrid)
        off3d = _1d_to_3d(off1d,nw)
        d3d = c3d + off3d - 1 # c3d is the global ijk (for 0 to ngrid-1) in 3D space, off3d is local ijk (for 0 to 2) and then -1 centers it

        # periodic neighbor index wrap
        Xoff = np.zeros(3, dtype=dtype)
        for j in range(3):
            if d3d[j] >= ngrid[j]:
                d3d[j] -= ngrid[j]
                Xoff[j] -= box
                skip = True
            if d3d[j] < 0:
                d3d[j] += ngrid[j]
                Xoff[j] += box
                skip = True
        if not periodic and skip: continue
        
        # 1d neighbor index
        d = d3d[0]*ngrid[1]*ngrid[2] + d3d[1]*ngrid[2] + d3d[2]

        nsecondary = s[d+1] - s[d]
        if nsecondary == 0:
            continue

        _do_cell_pairwise_vel(psort[s[c]:s[c+1]],
                              psort[s[d]:s[d+1]],
                              vsort[s[c]:s[c+1]],
                              vsort[s[d]:s[d+1]],
                              Rmax, nbin, Xoff,
                              thread_counts[t],
                              thread_weight_counts[t],
                              thread_norm_counts[t],
        )
    
    counts = thread_counts.sum(axis=0)
    weight_counts = thread_weight_counts.sum(axis=0)
    norm_counts = thread_norm_counts.sum(axis=0)
    
    # no self-counts
    counts[0] -= len(psort)

    pairwise = np.zeros(nbin, dtype=dtype)
    for i in range(nbin):
        if norm_counts[i] != zero:
            pairwise[i] = weight_counts[i]/norm_counts[i]

    return counts, pairwise


def numba_2pcf(pos, box, Rmax, nbin, nthread=-1, n1djack=None, pg_kwargs=None,
        corrfunc=False):
    '''
    Compute the 2PCF, and optionally jackknife.
    Assumes a periodic box and autocorrelation.

    Parameters
    ----------
    pos: ndarray, shape (N,3)
        The particle positions, in domain [0,box)

    box: float
        The box size

    Rmax: float
        The maximum radius of the 2PCF measurement

    nbin: int
        The number of linear radial 2PCF bins

    nthread: int, optional
        Number of threads to use (parallelized over cell pairs).
        Default of -1 means to use the numba default.

    n1djack: int, optional
        Number of jackknife patches per dimension. Patches
        are sub-cubes. Default of None means to not do jackknife.

    pg_kwargs: dict, optional
        Any keyword arguments to pass directly to the `particle_grid`
        function. Default: None.

    corrfunc: bool, optional
        Use Corrfunc instead. Useful for debugging.
        Default: False.
    '''
    if pg_kwargs is None:
        pg_kwargs = {}
    pg_kwargs = pg_kwargs.copy()
    if 'nthread' not in pg_kwargs:
        pg_kwargs['nthread'] = nthread
    if 'sort_in_cell' not in pg_kwargs:
        pg_kwargs['sort_in_cell'] = True
    
    if nthread == -1:
        nthread = nb.get_num_threads()
        
    # coerce inputs to match pos type
    box = pos.dtype.type(box)
    Rmax = pos.dtype.type(Rmax)
    edges = np.linspace(0,Rmax,nbin+1)
    
    if not corrfunc:
        ngrid = int(np.floor(box/Rmax))
        ngrid = max(ngrid,3)  # so that neighbors are always unique
        ngrid = (ngrid,)*3
        ngrid = np.atleast_1d(ngrid)
        
        psort, offsets = particle_grid.particle_grid(pos, ngrid, box, **pg_kwargs)

        nb.set_num_threads(nthread)
        counts = _2pcf(psort, offsets, ngrid, box, Rmax, nbin)
    else:
        import Corrfunc.theory.DD
        res = Corrfunc.theory.DD(1, nthread, edges, *pos.T, boxsize=box, periodic=True)
        counts = res['npairs']
        counts[0] -= len(pos)
        

    # compute xi from pairs
    N = len(pos)
    RR = np.diff(edges**3) * 4/3*np.pi * N*(N-1)/box**3
    xi = counts/RR - 1
    
    t = Table(dict(rmin=edges[:-1],
                   rmax=edges[1:],
                   rmid=(edges[1:] + edges[:-1])/2,
                   xi=xi,
                   npairs=counts,
                  ),
                meta=dict(corrfunc=corrfunc))
    
    if n1djack:
        jack = jackknife(n1djack, pos, box, Rmax, nbin, nthread=nthread, pg_kwargs=pg_kwargs,
                            corrfunc=corrfunc)
        for col in jack.colnames:
            if col in t.colnames:
                del jack[col]
        t = astropy.table.hstack((t,jack))
        
    return t

def numba_pairwise_vel(pos, vel, box, Rmax, nbin, nthread=-1, n1djack=None, pg_kwargs=None,
                       corrfunc=False, periodic=False):
    '''
    Compute the 2PCF, and optionally jackknife.
    Assumes a periodic box and autocorrelation.

    Parameters
    ----------
    pos: ndarray, shape (N,3)
        The particle positions, in domain [0,box)

    box: float
        The box size

    Rmax: float
        The maximum radius of the 2PCF measurement

    nbin: int
        The number of linear radial 2PCF bins

    nthread: int, optional
        Number of threads to use (parallelized over cell pairs).
        Default of -1 means to use the numba default.

    n1djack: int, optional
        Number of jackknife patches per dimension. Patches
        are sub-cubes. Default of None means to not do jackknife.

    pg_kwargs: dict, optional
        Any keyword arguments to pass directly to the `particle_grid`
        function. Default: None.

    corrfunc: bool, optional
        Use Corrfunc instead. Useful for debugging.
        Default: False.
    '''
    if pg_kwargs is None:
        pg_kwargs = {}
    pg_kwargs = pg_kwargs.copy()
    if 'nthread' not in pg_kwargs:
        pg_kwargs['nthread'] = nthread
    if 'sort_in_cell' not in pg_kwargs:
        pg_kwargs['sort_in_cell'] = True
    
    if nthread == -1:
        nthread = nb.get_num_threads()
        
    # coerce inputs to match pos type
    box = pos.dtype.type(box)
    Rmax = pos.dtype.type(Rmax)
    edges = np.linspace(0,Rmax,nbin+1)
    
    if not corrfunc:
        ngrid = int(np.floor(box/Rmax))
        ngrid = max(ngrid,3)  # so that neighbors are always unique
        ngrid = (ngrid,)*3
        ngrid = np.atleast_1d(ngrid)
        
        psort, vsort, offsets = particle_grid.pv_grid(pos, vel, ngrid, box, **pg_kwargs)

        nb.set_num_threads(nthread)
        counts, pairwise = _pairwise(psort, vsort, offsets, ngrid, box, Rmax, nbin, periodic)
    else:
        import Corrfunc.theory.DD
        res = Corrfunc.theory.DD(1, nthread, edges, *pos.T, boxsize=box, periodic=periodic)
        counts = res['npairs']
        counts[0] -= len(pos) # what if bin not starting at zero? B.H.
        pairwise = np.zeros_like(counts)

    # compute xi from pairs
    #N = len(pos)
    #RR = np.diff(edges**3) * 4/3*np.pi * N*(N-1)/box**3
    #xi = counts/RR - 1
    
    t = Table(dict(rmin=edges[:-1],
                   rmax=edges[1:],
                   rmid=(edges[1:] + edges[:-1])/2,
                   pairwise=pairwise,
                   npairs=counts,
                  ),
                meta=dict(corrfunc=corrfunc))
    
    if n1djack: # B.H. todo
        jack = jackknife(n1djack, pos, box, Rmax, nbin, nthread=nthread, pg_kwargs=pg_kwargs,
                            corrfunc=corrfunc)
        for col in jack.colnames:
            if col in t.colnames:
                del jack[col]
        t = astropy.table.hstack((t,jack))
        
    return t


def jackknife(n1djack, pos, box, Rmax, nbin, nthread=-1, corrfunc=False, pg_kwargs=None):
    # use the chaining mesh to generate patches
    psort, offsets = particle_grid.particle_grid(pos, n1djack, box, nthread=nthread)
    del pos  # careful!
    occ = np.diff(offsets)
    
    all_res = []
    njack = n1djack**3
    for i in range(njack):
        pos_drop1 = np.empty((len(psort) - occ[i],3), dtype=psort.dtype)
        # copy all before the dropped patch, then all after
        pos_drop1[:offsets[i]] = psort[:offsets[i]]
        pos_drop1[offsets[i]:] = psort[offsets[i+1]:]
        
        res = numba_2pcf(pos_drop1, box, Rmax, nbin, nthread=nthread, pg_kwargs=pg_kwargs,
                            corrfunc=corrfunc)
        all_res += [res]
        
    jackres = all_res[0]['rmin','rmax','rmid'].copy()
    jackres['jack_xi'] = np.vstack([t['xi'] for t in all_res]).T
    jackres['jack_mean'] = jackres['jack_xi'].mean(axis=1)
    diff = jackres['jack_xi'] - jackres['jack_mean'].reshape(-1,1)
    jackres['jack_cov'] = (diff @ diff.T) * (njack - 1) / njack
    
    jackres['jack_cor'] = jackres['jack_cov'] / jackres['jack_cov'].diagonal()**0.5
    jackres['jack_cor'] /= (jackres['jack_cov'].diagonal()**0.5).reshape(-1,1)
    
    return jackres
