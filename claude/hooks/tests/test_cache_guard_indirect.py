"""cache_guard: the delete forms that never name the path to `rm` (Auditor + Strategy, 2026-10-21 follow-up to #168).
`xargs rm`, `find -exec rm`, `find -delete` and Python `shutil.rmtree` all passed because the target is not a word after `rm`."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import cache_guard as g  # noqa: E402

CWD = "/Users/x/Developer/frankendom-deploy"
HOME = str(Path.home())

DENY = [
    "find ~/Library/Caches -name '*' -delete",
    "find ~/Library/Caches/ms-playwright -type f -delete",
    "find ~/.cache -mindepth 1 -maxdepth 1 -exec rm -rf {} +",
    "find ~/.cache -mtime +7 -exec rm -rf {} \\;",
    "find / -name ms-playwright -exec rm -rf {} +",
    "find ~ -name 'chromium-*' -execdir rm -rf {} +",
    "ssh root@49.12.7.18 'find /root/.cache -maxdepth 1 -delete'",
    "ls ~/Library/Caches | xargs rm -rf",
    "ls -d ~/Library/Caches/* | xargs -I{} rm -rf {}",
    "find ~/Library/Caches -maxdepth 1 | xargs rm -rf",
    "cat list.txt | xargs rm -rf",
    "echo ~/Library/Caches/ms-playwright | xargs trash",
    "python3 -c \"import shutil,os; shutil.rmtree(os.path.expanduser('~/Library/Caches/ms-playwright'))\"",
    "python3 -c \"import shutil; shutil.rmtree('/root/.cache/ms-playwright')\"",
    "python3 -c \"import shutil,pathlib; shutil.rmtree(pathlib.Path.home()/'.cache')\"",
    "python3 - <<'EOF'\nimport shutil, os\nfor d in os.listdir(os.path.expanduser('~/Library/Caches')):\n    shutil.rmtree(os.path.join(os.path.expanduser('~/Library/Caches'), d))\nEOF",
    "python -c \"import os; os.remove('/Users/x/Developer/.ms-playwright/a')\"",
    # Auditor 2026-10-21: the shell quotes around the -c argument hid the inner literal path
    f"python3 -c \"import shutil; shutil.rmtree('{HOME}/Library/Caches')\"",
    f"python3 -c 'import shutil; shutil.rmtree(\"{HOME}/Library/Caches\")'",
    f"python3 -c \"import shutil; shutil.rmtree('{HOME}/.cache')\"",
    f"python3 -c \"from shutil import rmtree as r; r('{HOME}/Library/Caches')\"",
    f"python3 -c \"import os; os.system('rm -rf {HOME}/Library/Caches')\"",
    f"python3 -c \"import subprocess; subprocess.run(['rm','-rf','{HOME}/Library/Caches'])\"",
    f"python3 -c \"import subprocess; subprocess.run('find {HOME}/.cache -delete', shell=True)\"",
    # Lead's ruling via the Auditor: node and perl one-liners are judged like python
    f"node -e \"require('fs').rmSync('{HOME}/Library/Caches',{{recursive:true}})\"",
    f"node -e 'fs.rmSync(\"{HOME}/.cache\",{{recursive:true,force:true}})'",
    "node -e \"fs.rmSync(require('os').homedir()+'/x',{recursive:true})\"",
    "node -e \"fs.rmSync(path.join(process.env.HOME,'.cache'),{recursive:true})\"",
    f"perl -e 'use File::Path qw(remove_tree); remove_tree(\"{HOME}/.cache\")'",
    f"perl -MFile::Path=rmtree -e 'rmtree(\"{HOME}/Library/Caches\")'",
    "perl -e 'unlink glob \"$ENV{HOME}/.cache/*\"'",
    f"perl -e 'system(\"rm -rf {HOME}/Library/Caches\")'",
    "python3 -c \"import shutil,os; shutil.rmtree(os.path.join('/root', '.cache'))\"",
    # Auditor @dc2e6b7: node's async deletes with a literal cache path
    f"node -e \"require('fs').rm('{HOME}/.cache',{{recursive:true}},()=>{{}})\"",
    f"node -e \"require('fs').promises.rm('{HOME}/Library/Caches',{{recursive:true}})\"",
    f"node -e \"const fsp=require('fs/promises'); fsp.rm('{HOME}/.cache',{{recursive:true}})\"",
    f"node -e \"require('rimraf').sync('{HOME}/.cache')\"",
    # Auditor, same rule: a path built from pieces cannot be shown clear of the cache, so a delete with it is denied
    f"python3 -c \"import shutil; shutil.rmtree('{HOME}/Libr'+'ary/Caches')\"",
    "python3 -c \"import shutil; d='/root'; shutil.rmtree(f'{d}/.cache')\"",
    "python3 -c \"import shutil; shutil.rmtree('{}/.cache'.format('/root'))\"",
    "python3 -c \"import shutil; shutil.rmtree('%s/.cache' % '/root')\"",
    "python3 -c \"import shutil; shutil.rmtree('/'.join(['', 'root', '.cache']))\"",
    "node -e 'fs.rmSync(`${\"/root\"}/.cache`,{recursive:true})'",
    "perl -e 'use File::Path; rmtree(\"/ro\".\"ot/.cache\")'",
    # Auditor: xargs is pipe-fed only after a `|`; a separator or a stdin redirect leaves the input unknown
    "echo /tmp/x; xargs rm -rf < list.txt",
    "xargs rm -rf < list.txt",
    "echo /tmp/x && xargs rm -rf",
    "echo /tmp/x | xargs rm -rf < list.txt",
]
ALLOW = [
    "find ~/Library/Caches/semble -type f -delete",
    "find ~/.cache/uv -mtime +7 -exec rm -rf {} +",
    "find /tmp/scratch -name '*.log' -delete",
    "find ~/Library/Caches -name '*.log' -print",
    "find ~/.cache -maxdepth 1 -type d",
    "ls /tmp/scratch | xargs rm -rf",
    "find /tmp/scratch -name '*.tmp' | xargs rm",
    "git ls-files -z | xargs -0 grep foo",
    "ls ~/Library/Caches | xargs echo",
    "python3 -c \"import shutil; shutil.rmtree('/tmp/scratch')\"",
    "python3 -c \"print('hello')\"",
    "python3 -c \"import subprocess; subprocess.run(['rm','-rf','/tmp/scratch'])\"",
    "python3 -c \"import os; os.system('rm -rf /tmp/scratch')\"",
    "echo /tmp/x | xargs rm -rf",
    "node -e \"fs.rmSync('/tmp/scratch',{recursive:true})\"",
    "node -e \"console.log(1)\"",
    "node build.js",
    "perl -e 'unlink \"/tmp/scratch/a.txt\"'",
    "perl -e 'print 1'",
    "python3 -m pytest tests -q",
    "echo \"shutil.rmtree('/root/.cache/ms-playwright')\"",
    "git commit -m 'close find -delete and xargs rm in cache_guard'",
]


def test_denies_indirect_deletes_of_the_cache_or_its_parents():
    for command in DENY:
        assert g.cache_delete_reason(command, CWD), command


def test_allows_indirect_deletes_that_cannot_reach_the_cache():
    for command in ALLOW:
        assert g.cache_delete_reason(command, CWD) == "", command
