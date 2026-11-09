{
  pkgs ? import ../../utils/default/pkgs.nix,
  os ? import ../../utils/default/os.nix,
  arch ? pkgs.callPackage ../../utils/default/arch.nix { },
}:
let
  lock = import ../../../packages.lock.nix;
  callPackage = pkgs.lib.callPackageWith { inherit pkgs os arch; };
  nativeFile = callPackage ../../utils/native-file/default.nix { };
  crossFile = callPackage ../../utils/cross-file/default.nix { };
  source = callPackage ../../utils/fetch-tarball/default.nix {
    name = "libplacebo-source-${lock.libplacebo.version}";
    inherit (lock.libplacebo) url sha256;
  };
  fastFloat = callPackage ../../utils/fetch-tarball/default.nix {
    name = "fast-float-source-${lock.fast_float.version}";
    inherit (lock.fast_float) url sha256;
  };
  python = pkgs.python3.withPackages (p: [ p.glad2 p.jinja2 ]);
  vulkanHeaders = callPackage ../../utils/fetch-tarball/default.nix {
    name = "vulkan-headers-source-${lock.vulkan_headers.version}";
    inherit (lock.vulkan_headers) url sha256;
  };
in
pkgs.stdenvNoCC.mkDerivation {
  pname = "mk-libplacebo-${os}-${arch}";
  inherit (lock.libplacebo) version;
  src = source;
  nativeBuildInputs = [ pkgs.meson pkgs.ninja pkgs.pkg-config python ];
  dontUseMesonConfigure = true;
  postUnpack = ''
    chmod -R u+w "$sourceRoot"
    cp -R ${fastFloat}/include "$sourceRoot/3rdparty/fast_float/"
    cp -R ${vulkanHeaders}/include "$sourceRoot/3rdparty/Vulkan-Headers/"
    substituteInPlace "$sourceRoot/meson.build" \
      --replace-fail "import('python').find_installation()" "import('python').find_installation('${python}/bin/python3')"
  '';
  configurePhase = ''
    meson setup build . --native-file ${nativeFile} --cross-file ${crossFile} \
      --prefix=$out --libdir=lib --default-library=static \
      -Dbuildtype=release -Db_staticpic=true -Dwrap_mode=nodownload \
      -Dauto_features=disabled -Dopengl=enabled -Dgl-proc-addr=enabled \
      -Ddovi=enabled -Ddemos=false -Dtests=false -Dbench=false -Dfuzz=false
  '';
  buildPhase = "meson compile -C build";
  installPhase = ''
    meson install -C build
    # mpv links this C API from a C target; the static archive also contains C++.
    sed -i '/^Libs:/s/$/ -lc++/' $out/lib/pkgconfig/libplacebo.pc
  '';
}
