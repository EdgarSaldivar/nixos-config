import shutil
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).parents[1] / 'wolf/valheim-launch.sh'


@pytest.fixture
def launch(tmp_path):
    game = tmp_path / 'game with spaces'
    game.mkdir()
    executable = game / 'valheim.x86_64'
    executable.write_text('#!' + shutil.which('bash') + '\nprintf "vanilla:%s\\n" "$@"\n')
    executable.chmod(0o755)
    (game / 'start_game_bepinex.sh').write_text('printf "bepinex:%s\\n" "$@"\n')
    (game / 'doorstop_libs').mkdir()
    (game / 'doorstop_libs/libdoorstop_x64.so').touch()
    target = tmp_path / 'profile with spaces/BepInEx.Preloader.dll'
    target.parent.mkdir()
    target.touch()
    bootstrap = tmp_path / 'runtime'
    bootstrap.write_text('#!' + shutil.which('bash') + '\nprintf "runtime\\n"\nexec "$@"\n')
    bootstrap.chmod(0o755)
    def run(*args):
        return subprocess.run(['bash', str(SCRIPT), *map(str, args)], text=True, capture_output=True)
    return run, executable, target, bootstrap


def test_native_profile_inside_runtime_preserves_arguments(launch):
    run, executable, target, bootstrap = launch
    result = run(bootstrap, executable, '--doorstop-enabled', 'true',
                 '--doorstop-target-assembly', target, 'argument with spaces')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['runtime', *[
        'bepinex:' + str(x) for x in (executable, '--doorstop-enabled', 'true',
        '--doorstop-target-assembly', target, 'argument with spaces')]]


@pytest.mark.parametrize('extra', [[], ['--doorstop-enabled', 'false']])
def test_vanilla_passes_through(launch, extra):
    run, executable, _, bootstrap = launch
    result = run(bootstrap, executable, *extra)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith('runtime\nvanilla:')
    assert 'bepinex:' not in result.stdout


def test_missing_profile_fails_without_launching_vanilla(launch):
    run, executable, target, bootstrap = launch
    target.unlink()
    result = run(bootstrap, executable, '--doorstop-enabled', 'true',
                 '--doorstop-target-assembly', target)
    assert result.returncode == 1
    assert result.stdout == ''
