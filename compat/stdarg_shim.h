/* Shim for modern clang: pongoOS builds with -nostdlibinc, and the checkra1n
 * newlib fork ships no stdarg.h, so va_start/va_end resolve to implicit
 * function declarations. Bind them to the builtins. */
#ifndef PONGO_STDAPI_SHIM_H
#define PONGO_STDAPI_SHIM_H

#define va_start(ap, param) __builtin_va_start(ap, param)
#define va_end(ap) __builtin_va_end(ap)
#define va_arg(ap, type) __builtin_va_arg(ap, type)
#define va_copy(dest, src) __builtin_va_copy(dest, src)

#endif
