#!/bin/bash
# Install Node 20 from the official tarball into /root/node22 and symlink over node.
set -e
cd /tmp
if [ ! -x /root/node22/bin/node ]; then
  curl -fsSL -o node20.tar.xz https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz
  mkdir -p /root/node22
  tar -xJf node20.tar.xz -C /root/node22 --strip-components=1
fi
ln -sf /root/node22/bin/node /usr/local/bin/node
ln -sf /root/node22/bin/npm /usr/local/bin/npm 2>/dev/null || true
node --version
