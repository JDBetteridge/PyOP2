# This file is part of PyOP2
#
# PyOP2 is Copyright (c) 2012, Imperial College London and
# others. Please see the AUTHORS file in the main source directory for
# a full list of copyright holders.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * The name of Imperial College London or that of other
#       contributors may not be used to endorse or promote products
#       derived from this software without specific prior written
#       permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTERS
# ''AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDERS OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
# OF THE POSSIBILITY OF SUCH DAMAGE.

"""OP2 OpenMP backend."""

import os
import numpy as np
import math

from exceptions import *
from utils import *
from mpi import collective
from petsc_base import *
import host
import device
import plan as _plan
from subprocess import Popen, PIPE

# hard coded value to max openmp threads
_max_threads = 32
# cache line padding
_padding = 8


def _detect_openmp_flags():
    p = Popen(['mpicc', '--version'], stdout=PIPE, shell=False)
    _version, _ = p.communicate()
    if _version.find('Free Software Foundation') != -1:
        return '-fopenmp', 'gomp'
    elif _version.find('Intel Corporation') != -1:
        return '-openmp', 'iomp5'
    else:
        from warnings import warn
        warn('Unknown mpicc version:\n%s' % _version)
        return '', ''


class Arg(host.Arg):

    def c_vec_name(self, idx=None):
        return self.c_arg_name() + "_vec[%s]" % (idx or 'tid')

    def c_kernel_arg_name(self, idx=None):
        return "p_%s[%s]" % (self.c_arg_name(), idx or 'tid')

    def c_local_tensor_name(self):
        return self.c_kernel_arg_name(str(_max_threads))

    def c_vec_dec(self):
        return ";\n%(type)s *%(vec_name)s[%(arity)s]" % \
            {'type': self.ctype,
             'vec_name': self.c_vec_name(str(_max_threads)),
             'arity': self.map.arity}

    def padding(self):
        return int(_padding * (self.data.cdim / _padding + 1)) * \
            (_padding / self.data.dtype.itemsize)

    def c_reduction_dec(self):
        return "%(type)s %(name)s_l[%(max_threads)s][%(dim)s]" % \
            {'type': self.ctype,
             'name': self.c_arg_name(),
             'dim': self.padding(),
             # Ensure different threads are on different cache lines
             'max_threads': _max_threads}

    def c_reduction_init(self):
        if self.access == INC:
            init = "(%(type)s)0" % {'type': self.ctype}
        else:
            init = "%(name)s[i]" % {'name': self.c_arg_name()}
        return "for ( int i = 0; i < %(dim)s; i++ ) %(name)s_l[tid][i] = %(init)s" % \
            {'dim': self.padding(),
             'name': self.c_arg_name(),
             'init': init}

    def c_reduction_finalisation(self):
        d = {'gbl': self.c_arg_name(),
             'local': "%s_l[thread][i]" % self.c_arg_name()}
        if self.access == INC:
            combine = "%(gbl)s[i] += %(local)s" % d
        elif self.access == MIN:
            combine = "%(gbl)s[i] = %(gbl)s[i] < %(local)s ? %(gbl)s[i] : %(local)s" % d
        elif self.access == MAX:
            combine = "%(gbl)s[i] = %(gbl)s[i] > %(local)s ? %(gbl)s[i] : %(local)s" % d
        return """
        for ( int thread = 0; thread < nthread; thread++ ) {
            for ( int i = 0; i < %(dim)s; i++ ) %(combine)s;
        }""" % {'combine': combine,
                'dim': self.data.cdim}

    def c_global_reduction_name(self, count=None):
        return "%(name)s_l%(count)d[0]" % {
            'name': self.c_arg_name(),
            'count': count}

# Parallel loop API


@collective
def par_loop(kernel, it_space, *args):
    """Invocation of an OP2 kernel with an access descriptor"""
    return ParLoop(kernel, it_space, *args)


class JITModule(host.JITModule):

    ompflag, omplib = _detect_openmp_flags()
    _cppargs = [os.environ.get('OMP_CXX_FLAGS') or ompflag]
    _libraries = [os.environ.get('OMP_LIBS') or omplib]
    _system_headers = ['omp.h']

    _wrapper = """
void wrap_%(kernel_name)s__(PyObject* _boffset,
                            PyObject* _nblocks,
                            PyObject* _blkmap,
                            PyObject* _offset,
                            PyObject* _nelems,
                            %(wrapper_args)s
                            %(const_args)s
                            %(off_args)s) {

  int boffset = (int)PyInt_AsLong(_boffset);
  int nblocks = (int)PyInt_AsLong(_nblocks);
  int* blkmap = (int *)(((PyArrayObject *)_blkmap)->data);
  int* offset = (int *)(((PyArrayObject *)_offset)->data);
  int* nelems = (int *)(((PyArrayObject *)_nelems)->data);

  %(wrapper_decs)s;
  %(const_inits)s;
  %(local_tensor_decs)s;
  %(off_inits)s;

  #ifdef _OPENMP
  int nthread = omp_get_max_threads();
  #else
  int nthread = 1;
  #endif

  #pragma omp parallel shared(boffset, nblocks, nelems, blkmap)
  {
    int tid = omp_get_thread_num();
    %(interm_globals_decl)s;
    %(interm_globals_init)s;

    #pragma omp for schedule(static)
    for ( int __b = boffset; __b < boffset + nblocks; __b++ )
    {
      %(vec_decs)s;
      int bid = blkmap[__b];
      int nelem = nelems[bid];
      int efirst = offset[bid];
      for (int i = efirst; i < efirst+ nelem; i++ )
      {
        %(vec_inits)s;
        %(itspace_loops)s
        %(extr_loop)s
        %(zero_tmps)s;
        %(kernel_name)s(%(kernel_args)s);
        %(addtos_vector_field)s;
        %(apply_offset)s
        %(extr_loop_close)s
        %(itspace_loop_close)s
        %(addtos_scalar_field)s;
      }
    }
    %(interm_globals_writeback)s;
  }
}
"""

    def generate_code(self):

        # Most of the code to generate is the same as that for sequential
        code_dict = super(JITModule, self).generate_code()

        _reduction_decs = ';\n'.join([arg.c_reduction_dec()
                                     for arg in self._args if arg._is_global_reduction])
        _reduction_inits = ';\n'.join([arg.c_reduction_init()
                                      for arg in self._args if arg._is_global_reduction])
        _reduction_finalisations = '\n'.join(
            [arg.c_reduction_finalisation() for arg in self._args
             if arg._is_global_reduction])

        code_dict.update({'reduction_decs': _reduction_decs,
                          'reduction_inits': _reduction_inits,
                          'reduction_finalisations': _reduction_finalisations})
        return code_dict


class ParLoop(device.ParLoop, host.ParLoop):

    def _compute(self, part):
        fun = JITModule(self.kernel, self.it_space, *self.args)
        if not hasattr(self, '_jit_args'):
            self._jit_args = [None, None, None, None, None]
            for arg in self.args:
                if arg._is_mat:
                    self._jit_args.append(arg.data.handle.handle)
                else:
                    self._jit_args.append(arg.data._data)

                if arg._is_indirect or arg._is_mat:
                    maps = as_tuple(arg.map, Map)
                    for map in maps:
                        self._jit_args.append(map.values)

            for c in Const._definitions():
                self._jit_args.append(c.data)

            # offset_args returns an empty list if there are none
            self._jit_args.extend(self.offset_args())

        if part.size > 0:
            #TODO: compute partition size
            plan = self._get_plan(part, 1024)
            self._jit_args[2] = plan.blkmap
            self._jit_args[3] = plan.offset
            self._jit_args[4] = plan.nelems

            boffset = 0
            for c in range(plan.ncolors):
                nblocks = plan.ncolblk[c]
                self._jit_args[0] = boffset
                self._jit_args[1] = nblocks
                fun(*self._jit_args)
                boffset += nblocks

    def _get_plan(self, part, part_size):
        if self._is_indirect:
            plan = _plan.Plan(part,
                              *self._unwound_args,
                              partition_size=part_size,
                              matrix_coloring=True,
                              staging=False,
                              thread_coloring=False)
        else:
            # TODO:
            # Create the fake plan according to the number of cores available
            class FakePlan(object):

                def __init__(self, part, partition_size):
                    self.nblocks = int(math.ceil(part.size / float(partition_size)))
                    self.ncolors = 1
                    self.ncolblk = np.array([self.nblocks], dtype=np.int32)
                    self.blkmap = np.arange(self.nblocks, dtype=np.int32)
                    self.nelems = np.array([min(partition_size, part.size - i * partition_size) for i in range(self.nblocks)],
                                           dtype=np.int32)
                    self.offset = np.arange(part.offset, part.offset + part.size, partition_size, dtype=np.int32)

            plan = FakePlan(part, part_size)
        return plan

    @property
    def _requires_matrix_coloring(self):
        """Direct code generation to follow colored execution for global
        matrix insertion."""
        return True


def _setup():
    pass