import { lstatSync, readlinkSync } from "node:fs";
import { dirname, isAbsolute, join, resolve, sep } from "node:path";
import { resolveTrustedWindowsPowerShellExe } from "../lib/windows-elevation";
import type { CodexTokenRefusal, CodexTokenRefusalReporter } from "./codex-launch-diagnostics";

// Paths are environment data, never interpolated PowerShell source. The token is
// not read until this read-only subprocess and the config checks have succeeded.
const SCRIPT = String.raw`
$ErrorActionPreference='Stop'
try {
  $user=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  $trusted=@($user,'S-1-5-18','S-1-5-32-544')
  $installer='S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
  $sections=[System.Security.AccessControl.AccessControlSections]'Owner,Access'
  $paths=@($env:OCX_CODEX_OWNED_HOME,[System.IO.Path]::Combine($env:OCX_CODEX_OWNED_HOME,'config.toml'))
  $exactOwners=2
  $chain=$env:OCX_CODEX_PATH_CHAIN -eq '1'
  if ($chain) {
    $paths=ConvertFrom-Json -InputObject $env:OCX_CODEX_OWNED_PATHS
    if ($paths.Count -eq 0) { exit 1 }
    $exactOwners=0
  }
  if ($env:OCX_CODEX_NAMESPACE_CHECK -eq '1') {
    $systemDir=$env:OCX_CODEX_NAMESPACE_FIXTURE
    if ([string]::IsNullOrEmpty($systemDir)) {
      $programData=[Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
      if ([string]::IsNullOrEmpty($programData)) { exit 1 }
      $systemDir=[System.IO.Path]::Combine($programData,'OpenAI','Codex')
    }
    foreach ($file in @('config.toml','requirements.toml','managed_config.toml')) {
      try { $null=[System.IO.File]::GetAttributes([System.IO.Path]::Combine($systemDir,$file)); exit 3 }
      catch [System.IO.FileNotFoundException] { }
      catch [System.IO.DirectoryNotFoundException] { }
    }
    $existing=$systemDir
    while ($true) {
      try { $attributes=[System.IO.File]::GetAttributes($existing); break }
      catch [System.IO.FileNotFoundException] { }
      catch [System.IO.DirectoryNotFoundException] { }
      $existing=[System.IO.Path]::GetDirectoryName($existing)
      if ([string]::IsNullOrEmpty($existing)) { exit 1 }
    }
    if (($attributes -band [System.IO.FileAttributes]::Directory) -eq 0) { exit 1 }
    $paths=@($existing)
    $exactOwners=0
  }
  $parent=[System.IO.Directory]::GetParent($paths[0])
  while ($null -ne $parent) { $paths+= $parent.FullName; $parent=$parent.Parent }
  for ($i=0;$i -lt $paths.Count;$i++) {
    $path=$paths[$i]
    $reparse=([System.IO.File]::GetAttributes($path) -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    if (!$chain -and $reparse) { exit 1 }
    if ([System.IO.Directory]::Exists($path)) { $acl=[System.Security.AccessControl.DirectorySecurity]::new($path,$sections) }
    else { $acl=[System.Security.AccessControl.FileSecurity]::new($path,$sections) }
    $raw=[System.Security.AccessControl.RawSecurityDescriptor]::new($acl.GetSecurityDescriptorBinaryForm(),0)
    if ($i -lt $exactOwners -and $raw.Owner.Value -ne $user) { exit 1 }
    if ($i -ge $exactOwners -and $trusted -notcontains $raw.Owner.Value -and $raw.Owner.Value -ne $installer) { exit 1 }
    if ($null -eq $raw.DiscretionaryAcl -or ([int]$raw.ControlFlags -band 4) -eq 0) { exit 1 }
    foreach ($ace in $raw.DiscretionaryAcl) {
      if ($ace -isnot [System.Security.AccessControl.CommonAce] -or $ace.IsCallback) { exit 1 }
      if (([int]$ace.AceFlags -band 8) -ne 0) { continue }
      if ([int]$ace.AceType -eq 1) { continue }
      if ([int]$ace.AceType -ne 0) { exit 1 }
      $sid=$ace.SecurityIdentifier.Value
      if ($trusted -contains $sid) { continue }
      # TrustedInstaller is an OS authority on ancestors only, never on the two owned entries.
      if ($i -ge $exactOwners -and $sid -eq $installer) { continue }
      # Include generic writes/all, DELETE, WRITE_DAC, WRITE_OWNER, and directory DELETE_CHILD.
      $mask=[int64]$ace.AccessMask -band 4294967295
      $mutation=1343029590
      if (($chain -and $i -gt 0 -and !$reparse) -or
        (!$chain -and ($i -ge 2 -or ($exactOwners -eq 0 -and $i -ge 1)))) { $mutation=1343029312 }
      if (($mask -band $mutation) -ne 0) {
        if ($exactOwners -eq 0) { exit 2 }
        exit 1
      }
    }
  }
  exit 0
} catch { exit 1 }
`;
const ENCODED_SCRIPT = Buffer.from(SCRIPT, "utf16le").toString("base64");

function windowsOwnershipCheck(home: string, namespace: boolean, fixtureDirectory?: string, report?: CodexTokenRefusalReporter, paths?: readonly string[]): boolean {
  // Remove case aliases before constructing a Windows environment block. These
  // private protocol inputs must come from this call, never the caller's shell.
  const inherited = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(?:OCX_CODEX_OWNED_HOME|OCX_CODEX_NAMESPACE_CHECK|OCX_CODEX_NAMESPACE_FIXTURE|OCX_CODEX_PATH_CHAIN|OCX_CODEX_OWNED_PATHS)$/i.test(key)));
  const result = Bun.spawnSync([
    resolveTrustedWindowsPowerShellExe(), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", ENCODED_SCRIPT,
  ], { env: { ...inherited, OCX_CODEX_OWNED_HOME: home, OCX_CODEX_NAMESPACE_CHECK: namespace ? "1" : "0",
    OCX_CODEX_NAMESPACE_FIXTURE: fixtureDirectory, OCX_CODEX_PATH_CHAIN: paths ? "1" : "0",
    OCX_CODEX_OWNED_PATHS: JSON.stringify(paths) }, stdin: "ignore", stdout: "ignore", stderr: "ignore", timeout: 5_000, windowsHide: true });
  const safe = result.success && !result.exitedDueToTimeout;
  if (!safe && namespace) report?.(result.exitCode === 2 ? "systemNamespaceWritable" : result.exitCode === 3 ? "systemConfig" : "systemNamespace");
  return safe;
}

/** Inspect lexical entries as well as every link target; realpath alone loses replacement authority. */
export function codexLaunchProjectAliasIsOwned(path: string): boolean {
  if ((process.platform !== "win32")) return false;
  try {
    const paths = new Set<string>();
    const inspect = (entry: string): boolean => {
      if (paths.has(entry)) return true;
      const stat = lstatSync(entry), parent = dirname(entry);
      paths.add(entry);
      if (!stat.isSymbolicLink() && !stat.isDirectory() && !stat.isFile()) return false;
      if (stat.isSymbolicLink()) {
        const target = readlinkSync(entry);
        // Keep dot segments: collapsing link/../target could hide the link that redirects it.
        if (!inspect(isAbsolute(target) ? target : parent + sep + target)) return false;
      }
      return parent === entry || inspect(parent);
    };
    if (!inspect(resolve(path))) return false;
    return windowsOwnershipCheck("", false, undefined, undefined, [...paths]);
  } catch { return false; }
}

/** A missing system layer is safe only if another user cannot create it later. */
export function codexLaunchSystemNamespaceIsSafe(fixtureDirectory?: string, report?: CodexTokenRefusalReporter): boolean {
  if (process.platform !== "win32") { report?.("systemNamespace"); return false; }
  return windowsOwnershipCheck("", true, fixtureDirectory, report);
}

/** Excludes other-user writers; it cannot isolate processes with the same user identity. */
export function codexLaunchHomeIsOwned(home: string): boolean {
  if (process.platform !== "win32") return false;
  try {
    const directory = lstatSync(home), config = lstatSync(join(home, "config.toml"));
    if (!directory.isDirectory() || directory.isSymbolicLink() || !config.isFile() || config.isSymbolicLink()) return false;
    return windowsOwnershipCheck(home, false);
  } catch { return false; }
}
