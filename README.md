## 環境

Windows 11 (VS Code, Powershell)

## 環境構築
### 推奨される方法：venvを使う

```
$ python3 -m venv bottleMail-env
$ . bottileMail-env/bin/activate
```

venvはPythonが公式で提供しているものであるため、安心して使うことができます。

### 推奨されない方法：minicondaを使う
```
$ conda create -n bottleMail-env python=3.11
$ conda activate bottleMail-env
$ pip install fastapi uvicorn
```

anacondaとpipは全く互換性がないため、干渉する可能性があります。
なお、FastAPI や Uvicorn は PyPI（pip）での提供が主流。

### 仮想環境構築後

VS Code を使っている場合、Python interpreter を bottleMail-env に設定する

1. Ctrl+Shift+P → Python: Select Interpreter
2. bottleMail-env を選択

## サーバを起動
```
$ uvicorn server_api:app --host 0.0.0.0 --port 8000 --reload
```

## サーバのデータ管理
```bash
/ (プロジェクトルート)
  server.py
  ...
  data/
    ├── users.json
    └── letters.json
```

Expo Goのインストールごとに付与されるユニークなIDでユーザ識別を行う（IDの取得はクライエントが担う。サーバは受け取るだけ）。
各ユーザには以下の情報が紐づけられる。これらの情報は`user.json`に保存される。
```json
  "user-123456789abc": {
    "preferences": { "emotion": "明るい", "custom": "政治の話はやだ" },
    "receivedLetterIds": ["letter_xyz", "letter_pqr"],
    "sentLetterIds": ["letter_123"]
  },
```

各ユーザが送信、受信してきた手紙はユニークな手紙IDで管理する。手紙本体は`.txt`形式で`letter_contents`で保存される。

