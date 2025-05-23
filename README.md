## 環境
Windows 11 (VS Code, Powershell)

## 環境構築
```
$ conda create -n bottleMail-env python=3.11
$ conda activate bottleMail-env
$ pip install fastapi uvicorn
```

VS Code を使っている場合、Python interpreter を bottleMail-env に設定する

1. Ctrl+Shift+P → Python: Select Interpreter
2. bottleMail-env を選択

## サーバを起動
```
$ uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```
