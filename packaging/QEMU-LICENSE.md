# QEMU in this package

`libexec/qarm/qemu-aarch64` and `libexec/qarm/qemu-x86_64` are QEMU's user-mode emulators, **unmodified**,
built from QEMU's release tarball of the version named in `QEMU-VERSION` next to this file:

```
../configure --target-list=aarch64-linux-user,x86_64-linux-user --enable-plugins --disable-docs --disable-tools --disable-werror
```

QEMU is free software under the GNU General Public License, version 2 (see `COPYING` in QEMU's source;
some parts are under other GPL-compatible licenses). Its complete corresponding source, the very tarball the binaries were
built from, is published with every release of this package as `qemu-<version>.tar.xz`. It is also at
https://download.qemu.org.

The rest of this package (Q-ARM: the plugin, libqarm, the headers, the tools) is Quantag IT Solutions GmbH's,
under Apache-2.0 (`LICENSE`).
