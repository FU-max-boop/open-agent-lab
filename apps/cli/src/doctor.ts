import { platform, release } from "node:os";

export interface DoctorReport {
  ok: boolean;
  node: {
    actual: string;
    required: string;
    supported: boolean;
  };
  host: {
    platform: NodeJS.Platform;
    architecture: string;
    release: string;
  };
  capabilities: {
    deterministicSmoke: true;
    contentAddressedEvidence: true;
    networkRequired: false;
  };
}

function supportsRequiredNode(version: string): boolean {
  const [major = 0, minor = 0] = version.split(".").map((part) => Number.parseInt(part, 10));
  return major > 20 || (major === 20 && minor >= 19);
}

export function doctor(): DoctorReport {
  const supported = supportsRequiredNode(process.versions.node);
  return {
    ok: supported,
    node: {
      actual: process.versions.node,
      required: ">=20.19.0",
      supported,
    },
    host: {
      platform: platform(),
      architecture: process.arch,
      release: release(),
    },
    capabilities: {
      deterministicSmoke: true,
      contentAddressedEvidence: true,
      networkRequired: false,
    },
  };
}
