{
  description = "Pinned, command-line migration experiments on NixOS";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/da6dc0e7b26060f2f71ce3ef7088fb56ee44fa36";
    qemu-usb-11-1-3 = {
      url = "github:ziyifu225/qemu/005c61c0ef6f6ae615283440b4bb7be51b2c6ddd";
      flake = false;
    };
    qemu-keycodemapdb = {
      url = "github:qemu/keycodemapdb/f5772a62ec52591ff6870b7e8ef32482371f22c6";
      flake = false;
    };
    qemu-softfloat = {
      url = "github:qemu/berkeley-softfloat-3/b64af41c3276f97f0e181920400ee056b9c88037";
      flake = false;
    };
    qemu-testfloat = {
      url = "github:qemu/berkeley-testfloat-3/e7af9751d9f9fd3b47911f51a5cfd08af256a9ab";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      qemu-usb-11-1-3,
      qemu-keycodemapdb,
      qemu-softfloat,
      qemu-testfloat,
    }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      experiment = pkgs.callPackage ./experiments/qemu-physical-device/default.nix {
        qemuSource = qemu-usb-11-1-3;
        keycodemapdbSource = qemu-keycodemapdb;
        softfloatSource = qemu-softfloat;
        testfloatSource = qemu-testfloat;
      };
    in
    {
      packages.${system} = {
        qemu-physical-device = experiment.runner;
        qemu-physical-device-qemu = experiment.qemu;
        qemu-physical-device-guest = experiment.guest;
        qemu-physical-device-test = experiment.hardwareTest;
        default = experiment.hardwareTest;
      };
      apps.${system} = {
        qemu-physical-device = {
          type = "app";
          program = "${experiment.runner}/bin/qemu-physical-device";
          meta.description = "Same-host physical USB migration with pinned QEMU";
        };
        qemu-physical-device-test = {
          type = "app";
          program = "${experiment.hardwareTest}/bin/qemu-physical-device-test";
          meta.description = "Run the same-host integration test with a required real USB device";
        };
        default = self.apps.${system}.qemu-physical-device-test;
      };
      checks.${system} = {
        runner =
          pkgs.runCommand "migration-runner-tests"
            {
              nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.pytest ])) ];
            }
            ''
              cp -r ${./experiments/qemu-physical-device} ./experiment
              chmod -R u+w ./experiment
              cd experiment
              python -m unittest discover -s tests -v
              touch "$out"
            '';
      };
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          (pkgs.python3.withPackages (ps: [ ps.pytest ]))
          experiment.runner
          pkgs.git
          pkgs.usbutils
          pkgs.nixfmt
        ];
      };
      formatter.${system} = pkgs.nixfmt;
    };
}
