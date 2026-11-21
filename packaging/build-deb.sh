#!/bin/sh
# Builds dist/corral_<version>_all.deb. Needs uv and dpkg-deb.
set -eu

cd "$(dirname "$0")/.."
version=$(uv version --short)
maintainer=${DEB_MAINTAINER:-corral maintainers <root@localhost>}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
root=$work/root

uv build --wheel --out-dir "$work/wheel" >/dev/null

# The runner has no third-party dependencies, so the package is just the
# module unpacked from the wheel plus a launcher using the system Python.
install -d "$root/usr/lib/corral" "$root/usr/bin" "$root/etc/corral" \
    "$root/usr/lib/systemd/system" "$root/usr/share/doc/corral/examples" "$root/DEBIAN"
uv run --no-project python -m zipfile -e "$work"/wheel/*.whl "$work/unpacked"
cp -r "$work/unpacked/corral" "$root/usr/lib/corral/"

cat > "$root/usr/bin/corral" <<'EOF'
#!/usr/bin/python3
import sys

sys.path.insert(0, "/usr/lib/corral")
from corral.cli import main

sys.exit(main())
EOF
chmod 755 "$root/usr/bin/corral"

install -m 644 packaging/config.toml "$root/etc/corral/config.toml"
install -m 644 packaging/corral.service "$root/usr/lib/systemd/system/"
install -m 644 examples/.env examples/ticker.py examples/report.py "$root/usr/share/doc/corral/examples/"
# batch.py finds corral.metrics through this checkout (a path dependency),
# which an installed copy does not have, so it stays out.
grep -v '^batch:' examples/Procfile > "$root/usr/share/doc/corral/examples/Procfile"
chmod 644 "$root/usr/share/doc/corral/examples/Procfile"
demo=usr/share/doc/corral/examples/demo-project
install -d "$root/$demo/src/demo_project"
install -m 644 examples/demo-project/pyproject.toml "$root/$demo/"
install -m 644 examples/demo-project/src/demo_project/*.py "$root/$demo/src/demo_project/"
install -m 644 README.md "$root/usr/share/doc/corral/"
install -m 755 packaging/postinst packaging/prerm packaging/postrm "$root/DEBIAN/"
echo /etc/corral/config.toml > "$root/DEBIAN/conffiles"

cat > "$root/DEBIAN/control" <<EOF
Package: corral
Version: $version
Section: admin
Priority: optional
Architecture: all
Depends: python3 (>= 3.11), adduser
Maintainer: $maintainer
Description: Procfile process supervisor with a web interface
 Runs the processes listed in Procfiles (for example Python scripts with
 "uv run --script"), either as restarted services or on a schedule, captures
 their output, and serves a web page with their status, searchable logs,
 Prometheus metrics and dashboards.
EOF

find "$root" -type d -exec chmod 755 {} +
find "$root/usr/lib/corral" -type f -exec chmod 644 {} +
mkdir -p dist
dpkg-deb --root-owner-group --build "$root" "dist/corral_${version}_all.deb"
