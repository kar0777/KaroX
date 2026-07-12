#!/usr/bin/env python3
from __future__ import annotations
import sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from product_doctor import resolve_server_dir

def main()->int:
    with tempfile.TemporaryDirectory() as temp:
        base=Path(temp)
        normal=base/"normal"/"server"; normal.mkdir(parents=True); (normal/"repo_tools.py").write_text("app=None\n",encoding="utf-8")
        assert resolve_server_dir(base/"normal")==normal.resolve()
        nested=base/"nested"/"server"/"server"; nested.mkdir(parents=True); (nested/"repo_tools.py").write_text("app=None\n",encoding="utf-8")
        assert resolve_server_dir(base/"nested")==nested.resolve()
    print("KaroX doctor layout resolution checks passed")
    return 0
if __name__=="__main__": raise SystemExit(main())
