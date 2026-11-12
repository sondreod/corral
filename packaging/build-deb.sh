#!/bin/sh
# Builds dist/scriptrunner_<version>_all.deb. Needs uv and dpkg-deb.
set -eu

cd "$(dirname "$0")/.."
version=$(uv version --short)
maintainer=${DEB_MAINTAINER:-scriptrunner maintainers <root@localhost>}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
root=$work/root

uv build --wheel --out-dir "$work/wheel" >/dev/null

# The runner has no third-party dependencies, so the package is just the
# module unpacked from the wheel plus a launcher using the system Python.
install -d "$root/usr/lib/scriptrunner" "$root/usr/bin" "$root/etc/scriptrunner/scripts" \
    "$root/usr/lib/systemd/system" "$root/usr/share/doc/scriptrunner/examples" "$root/DEBIAN"
uv run --no-project python -m zipfile -e "$work"/wheel/*.whl "$work/unpacked"
cp -r "$work/unpacked/scriptrunner" "$root/usr/lib/scriptrunner/"

cat > "$root/usr/bin/scriptrunner" <<'EOF'
#!/usr/bin/python3
import sys

sys.path.insert(0, "/usr/lib/scriptrunner")
from scriptrunner.cli import main

sys.exit(main())
EOF
chmod 755 "$root/usr/bin/scriptrunner"

install -m 644 packaging/config.toml "$root/etc/scriptrunner/config.toml"
install -m 644 packaging/scriptrunner.service "$root/usr/lib/systemd/system/"
install -m 644 examples/ticker.py examples/report.py "$root/usr/share/doc/scriptrunner/examples/"
demo=usr/share/doc/scriptrunner/examples/demo-project
install -d "$root/$demo/src/demo_project"
install -m 644 examples/demo-project/pyproject.toml "$root/$demo/"
install -m 644 examples/demo-project/src/demo_project/*.py "$root/$demo/src/demo_project/"
install -m 644 README.md "$root/usr/share/doc/scriptrunner/"
install -m 755 packaging/postinst packaging/prerm packaging/postrm "$root/DEBIAN/"
echo /etc/scriptrunner/config.toml > "$root/DEBIAN/conffiles"

cat > "$root/DEBIAN/control" <<EOF
Package: scriptrunner
Version: $version
Section: admin
Priority: optional
Architecture: all
Depends: python3 (>= 3.11), adduser
Maintainer: $maintainer
Description: supervisor for PEP 723 Python scripts with a web interface
 Runs Python scripts that declare their dependencies inline (PEP 723) via
 "uv run --script", either as restarted services or on a schedule, captures
 their output, and serves a web page with their status and searchable logs.
 .
 Requires the uv executable (https://docs.astral.sh/uv/) on the machine.
EOF

find "$root" -type d -exec chmod 755 {} +
find "$root/usr/lib/scriptrunner" -type f -exec chmod 644 {} +
mkdir -p dist
dpkg-deb --root-owner-group --build "$root" "dist/scriptrunner_${version}_all.deb"
