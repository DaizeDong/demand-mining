"""Generated native interpreter controls; scheduled-task APIs are synthetic stubs."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires Windows argument parsing")
SCRIPT = Path(__file__).parents[1] / "scripts/register-task.ps1"
POWERSHELL = shutil.which("powershell.exe")
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
  $_.Extent.Text -match '^\$arguments =|^if \(\$ConfigDir\)|^\$argline ='
})
if ($statements.Count -ne 3) { throw 'Expected exactly three argument-building statements' }
$build = [scriptblock]::Create(($statements.Extent.Text -join "`n"))
$encoded = @($inputs.valid | ForEach-Object { ConvertTo-WindowsArgument $_ })
$rejected = @($inputs.invalid | ForEach-Object {
  try { $null = ConvertTo-WindowsArgument $_; $false } catch { $true }
})
$actions = @(foreach ($withOptions in @($false, $true)) {
  $wrapper = $inputs.wrapper
  $Python = $inputs.python
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
    expected = ["-ExecutionPolicy", "Bypass", "-NoProfile", "-File", CASES["wrapper"],
                "-Python", CASES["python"]]
    if with_options:
        expected += ["-ConfigDir", CASES["config"]]
    assert native_arguments(encoded["actions"][int(with_options)]) == expected


TASK_HARNESS = r"""
param([string]$Source, [string]$SelectedPython, [string]$Selection, [string]$ConfigDir)
$ErrorActionPreference = 'Stop'
$global:SyntheticCapturedAction = $null
$global:SyntheticPythonPath = $SelectedPython
function Get-Command {
  param([string]$Name, [string]$ErrorAction)
  if ($Name -ne 'python') { throw 'Unexpected command discovery' }
  [pscustomobject]@{Source=$global:SyntheticPythonPath}
}
function New-ScheduledTaskAction {
  param([string]$Execute, [string]$Argument)
  [pscustomobject]@{Execute=$Execute; Argument=$Argument}
}
function New-ScheduledTaskTrigger { param([switch]$Daily, [string]$At) 'synthetic-trigger' }
function New-ScheduledTaskSettingsSet {
  param([switch]$StartWhenAvailable, [switch]$DontStopOnIdleEnd, [timespan]$ExecutionTimeLimit)
  'synthetic-settings'
}
function Register-ScheduledTask {
  param($TaskName, $Action, $Trigger, $Settings, $Description, [switch]$Force)
  $global:SyntheticCapturedAction = $Action
}
$failure = $null
try {
  if ($Selection -eq 'path') {
    & $Source -ConfigDir $ConfigDir 6>$null
  } else {
    & $Source -Python $SelectedPython -ConfigDir $ConfigDir 6>$null
  }
} catch { $failure = $_.Exception.Message }
[pscustomobject]@{action=$global:SyntheticCapturedAction; failure=$failure} |
  ConvertTo-Json -Depth 4 -Compress
"""


def registration(tmp_path, executable, selection):
    harness = tmp_path / "task-harness.ps1"
    harness.write_text(TASK_HARNESS, encoding="utf-8")
    selected = os.path.relpath(executable, tmp_path) if selection == "relative" else str(executable)
    result = subprocess.run([
        POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness),
        str(SCRIPT), selected, selection, CASES["config"]], cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("selection", ["explicit", "relative", "path"])
def test_registration_records_absolute_interpreter(tmp_path, selection):
    executable = tmp_path / "Synthetic Runtime/python.exe"
    executable.parent.mkdir()
    executable.touch()
    result = registration(tmp_path, executable, selection)
    assert result["failure"] is None
    assert result["action"]["Execute"] == "powershell.exe"
    assert native_arguments(result["action"]["Argument"]) == [
        "-ExecutionPolicy", "Bypass", "-NoProfile", "-File", str(SCRIPT.with_name("wrapper.ps1")),
        "-Python", str(executable.resolve()), "-ConfigDir", CASES["config"]]


@pytest.mark.parametrize("selection", ["explicit", "relative", "path"])
def test_registration_refuses_store_alias(tmp_path, selection):
    executable = tmp_path / "WindowsApps/python.exe"
    executable.parent.mkdir()
    executable.touch()
    result = registration(tmp_path, executable, selection)
    assert result["action"] is None
    assert "Windows Store" in result["failure"]


@pytest.mark.parametrize("selection", ["explicit", "path"])
def test_registration_refuses_missing_interpreter(tmp_path, selection):
    result = registration(tmp_path, tmp_path / "missing-python.exe", selection)
    assert result["action"] is None
    assert "Python executable not found" in result["failure"]


def run_wrapper(tmp_path, executable, selection, *arguments):
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_bytes(SCRIPT.with_name("wrapper.ps1").read_bytes())
    env = os.environ.copy()
    command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)]
    if selection == "path":
        env["PATH"] = str(executable.parent)
    else:
        selected = os.path.relpath(executable, tmp_path) if selection == "relative" else str(executable)
        command += ["-Python", selected]
    return subprocess.run(command + list(arguments), cwd=tmp_path, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=30)


@pytest.mark.parametrize("selection", ["explicit", "relative", "path"])
def test_wrapper_refuses_store_alias(tmp_path, selection):
    executable = tmp_path / "WindowsApps/python.exe"
    executable.parent.mkdir()
    executable.touch()
    result = run_wrapper(tmp_path, executable, selection)
    assert result.returncode != 0
    assert "Windows Store" in result.stderr


@pytest.mark.parametrize("selection", ["explicit", "relative", "path"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_wrapper_preserves_arguments_and_exit_code(tmp_path, selection, exit_code):
    caller = tmp_path / "scheduled.py"
    caller.write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"
        "print('synthetic caller stderr', file=sys.stderr)\n"
        f"raise SystemExit({exit_code})\n", encoding="utf-8")
    log_dir = str(tmp_path / "Synthetic Logs")
    result = run_wrapper(tmp_path, Path(sys.executable), selection,
                         "-ConfigDir", CASES["config"], "-LogDir", log_dir)
    assert result.returncode == exit_code, result.stderr
    assert json.loads(result.stdout) == ["--config-dir", CASES["config"], "--log-dir", log_dir]
    assert "synthetic caller stderr" in result.stderr
    assert not Path(log_dir).exists()
