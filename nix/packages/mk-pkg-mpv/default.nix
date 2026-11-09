{
  pkgs ? import ../../utils/default/pkgs.nix,
  os ? import ../../utils/default/os.nix,
  arch ? pkgs.callPackage ../../utils/default/arch.nix { },
  variant ? import ../../utils/default/variant.nix,
}:

let
  name = "mpv";
  packageLock = (import ../../../packages.lock.nix).${name};
  inherit (packageLock) version;

  variants = import ../../utils/constants/variants.nix;
  oses = import ../../utils/constants/oses.nix;
  callPackage = pkgs.lib.callPackageWith {
    inherit
      pkgs
      os
      arch
      variant
      ;
  };
  nativeFile = callPackage ../../utils/native-file/default.nix { };
  crossFile = callPackage ../../utils/cross-file/default.nix { };
  xctoolchainLipo = callPackage ../../utils/xctoolchain/lipo.nix { };
  ffmpeg = callPackage ../mk-pkg-ffmpeg/default.nix { };
  uchardet = callPackage ../mk-pkg-uchardet/default.nix { };
  libplacebo = callPackage ../mk-pkg-libplacebo/default.nix { };
  libass = callPackage ../mk-pkg-libass/default.nix { };

  nativeBuildInputs = [
    pkgs.meson
    pkgs.ninja
    pkgs.pkg-config
    pkgs.python3
    xctoolchainLipo
  ];

  pname = import ../../utils/name/package.nix name;
  src = callPackage ../../utils/fetch-tarball/default.nix {
    name = "${pname}-source-${version}";
    inherit (packageLock) url sha256;
  };
  patchedSource = pkgs.runCommand "${pname}-patched-source-${variant}-${version}" { } ''
    cp -r ${src} src
    export src=$PWD/src
    chmod -R 777 $src

    cd $src
    patch -p1 <${../../../patches/mpv-mobile-darwin.patch}
    patch -p1 <${../../../patches/mpv-audiounit-shared-session.patch}
    if [ "${variant}" == "${variants.audio}" ]; then
      patch -p1 <${../../../patches/mpv-remove-libass.patch}
    fi
    cd -

    cp -r $src $out
  '';
  fixedSource = callPackage ../../utils/patch-shebangs/default.nix {
    name = "${pname}-fixed-source-${variant}-${version}";
    src = patchedSource;
    inherit nativeBuildInputs;
  };
in

pkgs.stdenvNoCC.mkDerivation {
  name = "${pname}-${os}-${arch}-${variant}-${version}";
  pname = pname;
  inherit version;
  src = fixedSource;
  dontUnpack = true;
  enableParallelBuilding = true;
  inherit nativeBuildInputs;
  buildInputs =
    [ ffmpeg libplacebo ]
    ++ pkgs.lib.optionals (variant == "video") [
      uchardet
      libass
    ];
  configurePhase = ''
    export PKG_CONFIG_LIBDIR=""
    OPTIONS=(
      -Dauto_features=disabled -Dwrap_mode=nodownload
      -Dgpl=false -Dcplayer=false -Dlibmpv=true -Dbuild-date=false -Db_lundef=true
      -Dlua=disabled -Diconv=enabled -Dvector=enabled
    )
    if [ "${variant}" == "${variants.video}" ]; then
      OPTIONS+=(-Duchardet=enabled -Dzlib=enabled -Dgl=enabled -Dplain-gl=enabled)
    fi
    if [ "${os}" == "${oses.macos}" ]; then
      OPTIONS+=(-Dcoreaudio=enabled)
    else
      OPTIONS+=(-Daudiounit=enabled)
      if [ "${variant}" == "${variants.video}" ]; then
        OPTIONS+=(-Dios-gl=enabled)
      fi
    fi

    meson setup build $src \
      --native-file ${nativeFile} \
      --cross-file ${crossFile} \
      --prefix=$out \
      "''${OPTIONS[@]}" |
      tee configure.log
  '';
  buildPhase = ''
    meson compile -vC build
  '';
  installPhase = ''
    meson install -C build

    # copy configure.log
    mkdir -p $out/share/mpv
    cp configure.log $out/share/mpv/
  '';
}
