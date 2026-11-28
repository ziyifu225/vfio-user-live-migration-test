{
  pkgs,
  qemuSource,
  keycodemapdbSource,
  softfloatSource,
  testfloatSource,
}:
let
  revision = "005c61c0ef6f6ae615283440b4bb7be51b2c6ddd";
  qemu = pkgs.stdenv.mkDerivation {
    pname = "qemu-usb-migration";
    version = "11.1.3";
    src =
      assert qemuSource.rev == revision;
      qemuSource;
    strictDeps = true;
    nativeBuildInputs = with pkgs; [
      (python3.withPackages (ps: [
        ps.pycotap
        ps.qemu-qmp
        ps.setuptools
        ps.wheel
      ]))
      meson
      ninja
      pkg-config
    ];
    buildInputs = with pkgs; [
      glib
      pixman
      zlib
      libusb1
    ];
    dontUseMesonConfigure = true;
    configurePhase = ''
      runHook preConfigure
      test "$(cat VERSION)" = 11.1.3
      cp -r ${keycodemapdbSource} subprojects/keycodemapdb
      cp -r ${softfloatSource} subprojects/berkeley-softfloat-3
      cp -r ${testfloatSource} subprojects/berkeley-testfloat-3
      chmod -R u+w subprojects
      cp -r subprojects/packagefiles/berkeley-softfloat-3/. subprojects/berkeley-softfloat-3/
      cp -r subprojects/packagefiles/berkeley-testfloat-3/. subprojects/berkeley-testfloat-3/
      patchShebangs scripts
      mkdir build
      cd build
      ../configure --prefix="$out" \
        --target-list=x86_64-softmmu --without-default-features \
        --enable-tcg --enable-kvm --enable-libusb \
        --enable-trace-backends=log --disable-rust --disable-werror \
        --disable-download --disable-docs
      runHook postConfigure
    '';
    buildPhase = ''
      runHook preBuild
      ninja -j "$NIX_BUILD_CORES" qemu-system-x86_64
      runHook postBuild
    '';
    installPhase = ''
      runHook preInstall
      install -Dm755 qemu-system-x86_64 "$out/bin/qemu-system-x86_64"
      mkdir -p "$out/share/qemu"
      cp ../pc-bios/{bios-256k.bin,kvmvapic.bin,linuxboot_dma.bin,multiboot_dma.bin,pvh.bin} "$out/share/qemu/"
      printf '%s\n' '${revision}' > "$out/share/qemu/experiment-revision"
      runHook postInstall
    '';
  };
  kernel = pkgs.linuxPackages.kernel;
  modules = pkgs.makeModulesClosure {
    kernel = kernel.modules;
    firmware = pkgs.runCommand "empty-usb-firmware" { } ''mkdir -p "$out/lib/firmware"'';
    rootModules = [
      "xhci_pci"
      "usbhid"
      "hid_generic"
      "usb_storage"
      "uas"
    ];
  };
  busybox = pkgs.pkgsStatic.busybox;
  init = pkgs.writeScript "migration-guest-init" ''
    #!${busybox}/bin/sh
    export PATH=${pkgs.kmod}/bin:/bin
    mkdir -p /proc /sys /dev /tmp
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t devtmpfs devtmpfs /dev
    mount -t tmpfs tmpfs /tmp
    exec </dev/null >/dev/ttyS0 2>&1
    for module in xhci_pci usbhid hid_generic usb_storage uas; do
      modprobe "$module" || { echo "EXPERIMENT_MODULE_FAILED $module"; exec sleep infinity; }
    done
    boot=$(cat /proc/sys/kernel/random/boot_id)
    seq=0
    while :; do
      echo "EXPERIMENT_HEARTBEAT boot=$boot seq=$seq"
      for dev in /sys/bus/usb/devices/*; do
        [ -f "$dev/idVendor" ] && [ -f "$dev/idProduct" ] || continue
        config=$(cat "$dev/bConfigurationValue" 2>/dev/null)
        [ -n "$config" ] || continue
        echo "EXPERIMENT_USB boot=$boot seq=$seq vid=$(cat "$dev/idVendor") pid=$(cat "$dev/idProduct") devnum=$(cat "$dev/devnum") config=$config"
      done
      seq=$((seq + 1))
      sleep 1
    done
  '';
  initrd = pkgs.makeInitrd {
    name = "usb-migration-initrd";
    contents = [
      {
        object = init;
        symlink = "/init";
      }
      {
        object = busybox;
        symlink = "/bin";
        suffix = "/bin";
      }
      {
        object = modules;
        symlink = "/lib";
        suffix = "/lib";
      }
      {
        object = pkgs.kmod;
        symlink = "/tools/kmod";
      }
    ];
  };
  guest = pkgs.runCommand "usb-migration-guest" { } ''
    mkdir -p "$out"
    ln -s ${kernel}/bzImage "$out/kernel"
    ln -s ${initrd}/initrd "$out/initrd"
    printf '%s\n' '${kernel.version}' > "$out/kernel-version"
  '';
  runnerSource = pkgs.runCommand "qemu-physical-device-runner-source" { } ''
    mkdir -p "$out/hardware"
    cp ${./run.py} "$out/run.py"
    cp ${./hardware/experiment_paths.py} "$out/hardware/experiment_paths.py"
  '';
  runner = pkgs.writeShellApplication {
    name = "qemu-physical-device";
    runtimeInputs = [ pkgs.python3 ];
    text = ''
      export EXPERIMENT_QEMU=${qemu}/bin/qemu-system-x86_64
      export EXPERIMENT_GUEST=${guest}
      exec python3 ${runnerSource}/run.py "$@"
    '';
  };
  hardwareTest = pkgs.writeShellApplication {
    name = "qemu-physical-device-test";
    runtimeInputs = [ (pkgs.python3.withPackages (ps: [ ps.pytest ])) ];
    text = ''
      export EXPERIMENT_RUNNER=${runner}/bin/qemu-physical-device
      export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
      exec python3 -m pytest -p no:terminal -p no:cacheprovider ${./hardware} "$@"
    '';
  };
in
{
  inherit
    qemu
    guest
    runner
    hardwareTest
    ;
}
