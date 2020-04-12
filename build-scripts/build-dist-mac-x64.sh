#!/bin/bash

### This is intended to run within the rust-stretch docker image (or a debian-like system with required dependencies).

### This script uses osxcross [https://github.com/tpoechtrager/osxcross] to cross-compile from linux to MacOS.

set -e

cd "$(dirname "$(dirname "$0")")"

apt-get update
apt-get install -y clang

build_sdk () {
  apt-get update
  apt-get install -y clang cmake
  git clone https://github.com/blockstackpbc/osxcross --depth=1
  wget -N "https://github.com/blockstackpbc/osxcross/releases/download/v1/MacOSX10.14.sdk.tar.bz2" --directory-prefix=osxcross/tarballs/
  UNATTENDED=yes DISABLE_LTO_SUPPORT=1 OSX_VERSION_MIN=10.7 ./osxcross/build.sh
  cd ./osxcross
  BINARYPACKAGE=1 ./package.sh
}

fetch_extract_sdk() {
  wget -nc "https://github.com/blockstackpbc/osxcross/releases/download/v1/osxcross-e0a1718_xcode-v10.2.1.tar.xz"
  echo "Extracting osxcross package..."
  tar --checkpoint=25000 -xf  "osxcross-e0a1718_xcode-v10.2.1.tar.xz" -C /tmp
}

fetch_extract_sdk
rustup target add x86_64-apple-darwin

PATH="/tmp/osxcross/bin:$PATH" \
LD_LIBRARY_PATH="/tmp/osxcross/lib:$LD_LIBRARY_PATH" \
CC=o64-clang \
CXX=o64-clang++ \
LIBZ_SYS_STATIC=1 \
CC_x86_64_apple_darwin=x86_64-apple-darwin18-clang \
CARGO_TARGET_X86_64_APPLE_DARWIN_LINKER=x86_64-apple-darwin18-clang \
CARGO_TARGET_X86_64_APPLE_DARWIN_AR=x86_64-apple-darwin18-ar \
cargo build --target x86_64-apple-darwin --release
