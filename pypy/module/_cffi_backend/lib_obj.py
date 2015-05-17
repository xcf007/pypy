from rpython.rlib import jit
from rpython.rtyper.lltypesystem import lltype, rffi
from rpython.rlib.rdynload import DLLHANDLE

from pypy.interpreter.error import OperationError, oefmt
from pypy.interpreter.baseobjspace import W_Root
from pypy.interpreter.typedef import TypeDef
from pypy.interpreter.gateway import interp2app

from pypy.module._cffi_backend import parse_c_type, realize_c_type
from pypy.module._cffi_backend import cffi_opcode, cglob
from pypy.module._cffi_backend.realize_c_type import getop, getarg
from pypy.module._cffi_backend.cdataobj import W_CData
from pypy.module._cffi_backend.ctypefunc import W_CTypeFunc
from pypy.module._cffi_backend.structwrapper import W_StructWrapper


class W_LibObject(W_Root):
    libhandle = rffi.cast(DLLHANDLE, 0)    # the dlopen()ed handle, if any

    def __init__(self, ffi, libname):
        self.space = ffi.space
        self.ctx = ffi.ctxobj.ctx
        self.ffi = ffi
        self.dict_w = {}          # content, built lazily
        self.libname = libname    # some string that gives the name of the lib

    def descr_repr(self):
        return self.space.wrap("<Lib object for '%s'>" % self.libname)

    def make_includes_from(self, c_includes):
        space = self.space
        num = 0
        includes = []
        while c_includes[num]:
            include_name = rffi.charp2str(c_includes[num])
            try:
                w_lib1 = space.appexec([space.wrap(include_name)], """(modname):
                    mod = __import__(modname, None, None, ['ffi', 'lib'])
                    return mod.lib""")
                lib1 = space.interp_w(W_LibObject, w_lib1)
            except OperationError, e:
                if e.async(space):
                    raise
                raise oefmt(space.w_ImportError,
                    "while loading %s: failed to import ffi, lib from %s",
                    self.libname, include_name)
            includes.append((lib1.ffi, lib1))
            num += 1
        self.ffi.included_ffis_libs = includes[:]

    def _build_cpython_func(self, g):
        # Build a function: in the PyPy version, these are all equivalent
        # and 'g->address' is a pointer to a function of exactly the
        # C type specified --- almost: arguments that are structs or
        # unions are replaced with pointers, and a return value that
        # would be struct or union is instead handled by passing
        # inside the function a hidden first pointer argument.
        rawfunctype = realize_c_type.realize_c_type_or_func(
            self.ffi, self.ctx.c_types, getarg(g.c_type_op))
        assert isinstance(rawfunctype, realize_c_type.W_RawFuncType)
        #
        w_ct, locs = rawfunctype.unwrap_as_nostruct_fnptr(self.ffi)
        #
        ptr = rffi.cast(rffi.CCHARP, g.c_address)
        assert ptr
        w_cdata = W_CData(self.space, ptr, w_ct)
        if locs is not None:
            w_cdata = W_StructWrapper(w_cdata, locs, rawfunctype)
        return w_cdata

    @jit.elidable_promote()
    def _get_attr_elidable(self, attr):
        return self.dict_w[attr]     # KeyError if not found

    @jit.dont_look_inside
    def _build_attr(self, attr):
        index = parse_c_type.search_in_globals(self.ctx, attr)
        if index < 0:
            for ffi1, lib1 in self.ffi.included_ffis_libs:
                if lib1 is not None:
                    try:
                        w_result = lib1._get_attr_elidable(attr)
                        break           # found, break out of this loop
                    except KeyError:
                        w_result = lib1._build_attr(attr)
                        if w_result is not None:
                            break       # found, break out of this loop
                else:
                    w_result = ffi1.fetch_int_constant(attr)
                    if w_result is not None:
                        break           # found, break out of this loop
            else:
                return None     # not found at all
        else:
            space = self.space
            g = self.ctx.c_globals[index]
            op = getop(g.c_type_op)
            if (op == cffi_opcode.OP_CPYTHON_BLTN_V or
                op == cffi_opcode.OP_CPYTHON_BLTN_N or
                op == cffi_opcode.OP_CPYTHON_BLTN_O):
                # A function
                w_result = self._build_cpython_func(g)
                #
            elif op == cffi_opcode.OP_GLOBAL_VAR:
                # A global variable of the exact type specified here
                w_ct = realize_c_type.realize_c_type(
                    self.ffi, self.ctx.c_types, getarg(g.c_type_op))
                g_size = rffi.getintfield(g, 'c_size')
                if g_size != w_ct.size and g_size != 0 and w_ct.size > 0:
                    raise oefmt(self.ffi.w_FFIError,
                            "global variable '%s' should be %d bytes "
                            "according to the cdef, but is actually %d",
                            attr, w_ct.size, g_size)
                ptr = rffi.cast(rffi.CCHARP, g.c_address)
                if not ptr:   # for dlopen() style
                    ptr = self.cdlopen_fetch(attr)
                w_result = cglob.W_GlobSupport(space, w_ct, ptr)
                #
            elif (op == cffi_opcode.OP_CONSTANT_INT or
                  op == cffi_opcode.OP_ENUM):
                # A constant integer whose value, in an "unsigned long long",
                # is obtained by calling the function at g->address
                w_result = realize_c_type.realize_global_int(self.ffi, g,
                                                             index)
                #
            elif op == cffi_opcode.OP_CONSTANT:
                # A constant which is not of integer type
                w_ct = realize_c_type.realize_c_type(
                    self.ffi, self.ctx.c_types, getarg(g.c_type_op))
                fetch_funcptr = rffi.cast(
                    realize_c_type.FUNCPTR_FETCH_CHARP,
                    g.c_address)
                assert fetch_funcptr
                assert w_ct.size > 0
                with lltype.scoped_alloc(rffi.CCHARP.TO, w_ct.size) as ptr:
                    fetch_funcptr(ptr)
                    w_result = w_ct.convert_to_object(ptr)
                #
            else:
                raise oefmt(space.w_NotImplementedError,
                            "in lib_build_attr: op=%d", op)

        assert w_result is not None
        self.dict_w[attr] = w_result
        return w_result

    def _get_attr(self, w_attr):
        attr = self.space.str_w(w_attr)
        try:
            w_value = self._get_attr_elidable(attr)
        except KeyError:
            w_value = self._build_attr(attr)
            if w_value is None:
                raise oefmt(self.space.w_AttributeError,
                            "cffi library '%s' has no function, constant "
                            "or global variable named '%s'",
                            self.libname, attr)
        return w_value

    def descr_getattribute(self, w_attr):
        w_value = self._get_attr(w_attr)
        if isinstance(w_value, cglob.W_GlobSupport):
            w_value = w_value.read_global_var()
        return w_value

    def descr_setattr(self, w_attr, w_newvalue):
        w_value = self._get_attr(w_attr)
        if isinstance(w_value, cglob.W_GlobSupport):
            w_value.write_global_var(w_newvalue)
        else:
            raise oefmt(self.space.w_AttributeError,
                        "cannot write to function or constant '%s'",
                        self.space.str_w(w_attr))

    def descr_delattr(self, w_attr):
        self._get_attr(w_attr)    # for the possible AttributeError
        raise oefmt(self.space.w_AttributeError,
                    "C attribute cannot be deleted")

    def descr_dir(self):
        space = self.space
        total = rffi.getintfield(self.ctx, 'c_num_globals')
        g = self.ctx.c_globals
        names_w = [space.wrap(rffi.charp2str(g[i].c_name))
                   for i in range(total)]
        return space.newlist(names_w)

    def address_of_global_var(self, varname):
        # rebuild a string object from 'varname', to do typechecks and
        # to force a unicode back to a plain string
        space = self.space
        w_value = self._get_attr(space.wrap(varname))
        if isinstance(w_value, cglob.W_GlobSupport):
            # regular case: a global variable
            return w_value.address()
        #
        if ((isinstance(w_value, W_CData) and
                isinstance(w_value.ctype, W_CTypeFunc))
            or isinstance(w_value, W_StructWrapper)):
            # '&func' is 'func' in C, for a constant function 'func'
            return w_value
        #
        raise oefmt(space.w_AttributeError,
                    "cannot take the address of the constant '%s'", varname)

    def cdlopen_fetch(self, name):
        raise NotImplementedError


W_LibObject.typedef = TypeDef(
        'CompiledLib',
        __repr__ = interp2app(W_LibObject.descr_repr),
        __getattribute__ = interp2app(W_LibObject.descr_getattribute),
        __setattr__ = interp2app(W_LibObject.descr_setattr),
        __delattr__ = interp2app(W_LibObject.descr_delattr),
        __dir__ = interp2app(W_LibObject.descr_dir),
        )
W_LibObject.typedef.acceptable_as_base_class = False
