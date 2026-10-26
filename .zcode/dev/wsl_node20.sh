#!/bin/bash
# Install Node 20 from the official tarball into /root/node20 and symlink over node.
set -e
cd /tmp
if [ ! -x /root/node20/bin/node ]; then
  curl -fsSL -o node20.tar.xz https://nodejs.org/dist/v20.18.1/node-v20.18.1-linux-x64.tar.xz
  mkdir -p /root/node20
  tar -xJf node20.tar.xz -C /root/node20 --strip-components=1
fi
ln -sf /root/node20/bin/node /usr/local/bin/node
ln -sf /root/node20/bin/npm /usr/local/bin/npm 2>/dev/null || true
node --version
