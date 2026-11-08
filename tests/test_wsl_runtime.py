"""Command/permission regression tests; no WSL or candidate is executed."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agentforge.runtime import Runtime, RuntimeErrorSafe


class WslRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = {"api_key":"unit", "model":"deepseek-v4-pro", "base_url":"https://aigw.omniedu.com/v1",
                         "key_source":"test", "timeout":60, "image":"agentforge-test:py313", "docker_backend":"wsl", "wsl_distro":"AgentForge"}
        with patch('agentforge.runtime._settings', return_value=self.settings):
            self.runtime = Runtime(self.root, self.root / 'data')

    async def test_wsl_is_explicit_and_does_not_require_windows_docker(self):
        with patch('agentforge.runtime.shutil.which', return_value='wsl.exe') as which, patch.object(self.runtime,'_command',AsyncMock(side_effect=[(0,'linux\n',''),(0,'[]','')])) as command:
            health = await self.runtime.health()
        which.assert_called_once_with('wsl')
        self.assertTrue(health['sandbox']['available'])
        self.assertEqual(command.await_args_list[0].args[0], ['wsl','--distribution','AgentForge','--user','root','--exec','docker','info','--format','{{.OSType}}'])

    async def test_mount_paths_are_converted_without_shell(self):
        with patch.object(self.runtime,'_command',AsyncMock(return_value=(0,'/mnt/d/未来杯/path with space\n',''))) as command:
            converted = await self.runtime._mount_path(self.root)
        self.assertEqual(converted, '/mnt/d/未来杯/path with space')
        self.assertEqual(command.await_args.args[0][6:9], ['wslpath','-a','-u'])

    async def test_invalid_wsl_output_or_distro_never_falls_back(self):
        with patch.object(self.runtime,'_command',AsyncMock(return_value=(1,'','missing'))):
            with self.assertRaises(RuntimeErrorSafe):
                await self.runtime._mount_path(self.root)
        self.settings['wsl_distro'] = 'AgentForge; command'
        with self.assertRaises(RuntimeErrorSafe):
            self.runtime._docker_command('run')

    async def test_omniedu_v4_disables_thinking_in_real_adapter(self):
        response = {'choices':[{'message':{'content':'{"passed":true,"issues":[]}'}}], 'usage':{'total_tokens':2}}
        with patch.object(self.runtime,'_post',return_value=response) as post:
            await self.runtime.invoke({'role':'reviewer','tools':[]},{'mode':'live'}, {})
        self.assertEqual(post.call_args.args[0]['thinking'], {'type':'disabled'})
