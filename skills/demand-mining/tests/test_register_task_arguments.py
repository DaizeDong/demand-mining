"""Generated Windows argument controls; task registration code is never executed."""
import ctypes
import json
import os
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires Windows argument parsing")
SCRIPT = Path(__file__).parents[1] / "scripts/register-task.ps1"
CASES = json.loads((Path(__file__).parent / "fixtures/repair_cases.json").read_text(
    encoding="utf-8"))["windows_arguments"]
HARNESS = r"""
param([string]$Source, [string]$Cases)
$ErrorActionPreference = 'Stop'
$inputs = Get-Content -LiteralPath $Cases -Raw | ConvertFrom-Json
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
$functions = @($ast.FindAll({param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
  $node.Name -eq 'ConvertTo-WindowsArgument'
}, $false))
if ($functions.Count -ne 1) { throw 'Expected exactly one argument encoder' }
. ([scriptblock]::Create($functions[0].Extent.Text))
$statements = @($ast.EndBlock.Statements | Where-Object {
  $_.Extent.Text -match '^\$arguments =|^if \(\$Python\)|^if \(\$ConfigDir\)|^\$argline ='
})
if ($statements.Count -ne 4) { throw 'Expected exactly four argument-building statements' }
$build = [scriptblock]::Create(($statements.Extent.Text -join "`n"))
$encoded = @($inputs.valid | ForEach-Object { ConvertTo-WindowsArgument $_ })
$rejected = @($inputs.invalid | ForEach-Object {
  try { $null = ConvertTo-WindowsArgument $_; $false } catch { $true }
})
$actions = @(foreach ($withOptions in @($false, $true)) {
  $wrapper = $inputs.wrapper
  $Python = if ($withOptions) { $inputs.python } else { '' }
  $ConfigDir = if ($withOptions) { $inputs.config } else { '' }
  . $build
  $argline
})
[pscustomobject]@{encoded=$encoded;rejected=$rejected;actions=$actions} |
  ConvertTo-Json -Depth 4 -Compress
"""


@pytest.fixture(scope="module")
def encoded(tmp_path_factory):
    root = tmp_path_factory.mktemp("windows-arguments")
    harness, cases = root / "harness.ps1", root / "cases.json"
    harness.write_text(HARNESS, encoding="utf-8")
    cases.write_text(json.dumps(CASES), encoding="utf-8")
    result = subprocess.run([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(harness), str(SCRIPT), str(cases)], capture_output=True, text=True,
        encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def native_arguments(commandline):
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW("synthetic.exe " + commandline, ctypes.byref(count))
    assert argv, ctypes.get_last_error()
    try:
        return [argv[index] for index in range(1, count.value)]
    finally:
        kernel.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


@pytest.mark.parametrize("index", range(len(CASES["valid"])))
def test_each_argument_roundtrips_through_native_parser(encoded, index):
    assert native_arguments(encoded["encoded"][index]) == [CASES["valid"][index]]


@pytest.mark.parametrize("index", range(len(CASES["invalid"])))
def test_control_characters_are_rejected(encoded, index):
    assert encoded["rejected"][index] is True


@pytest.mark.parametrize("with_options", [False, True])
def test_task_action_arguments_preserve_separate_values(encoded, with_options):
    expected = ["-ExecutionPolicy", "Bypass", "-NoProfile", "-File", CASES["wrapper"]]
    if with_options:
        expected += ["-Python", CASES["python"], "-ConfigDir", CASES["config"]]
    assert native_arguments(encoded["actions"][int(with_options)]) == expected
