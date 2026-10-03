# 動作確認の範囲 / Testing scope

## ローカルで確認した環境

2026-10-03に、Windows、Python 3.11.9、PyQt6 6.11.0、keyring 25.7.0で、81件の自動テストが成功しました。この記録は当日のコードに対する結果であり、将来の版や別環境での保証ではありません。

- ROMとMRAはテスト内で人工的に生成し、一時フォルダーを使用します。
- GUIテストはoffscreen方式で実行します。手動での画面操作・全画面サイズの表示確認とは異なります。
- FTPテストは模擬実装を使用します。実MiSTer・実FTPサーバーに接続しません。
- 停電、プロセスの強制終了、複数プロセスの同時実行を網羅する試験ではありません。

公開準備では、別の一時フォルダーに公開予定ファイルをコピーし、新しいPython 3.11.9の仮想環境で`python -m pip install ".[gui]"`も確認しました。PyQt6 6.11.0 / Qt 6.11.2 / keyring 25.7.0で81件のテストが成功し、ソースフォルダー外からのインストール済みCLI起動とGPL-3.0-onlyのライセンス情報・LICENSE同梱を確認しました。

## GitHub Actions

Windows上のPython 3.10 / 3.11 / 3.13で、GUI依存を含むインストール、CLIの起動確認、全自動テストを実行する設定を用意しています。設定の追加だけでは、その環境でのテスト成功を意味しません。公開・push後にActionsの結果を確認してください。

## 実機での確認記録

この文書では実機試験の成功は未記録です。リリース前に次を記録してください。

| 項目 | 記録する内容 |
| --- | --- |
| 対象版 | タグまたはコミット |
| PC環境 | OS、Python、PyQt6 |
| MiSTer環境 | MiSTer・FTPサーバーの版、保存先構成 |
| MRA取得 | 初回取得、変更時の更新、未変更時の省略 |
| ZIP送信 | 新規、同一内容の省略、不足時の置換 |
| 不適合ZIP | 専用の試験用データで削除動作を確認 |
| 中止・復旧 | Run/Sendの中止、残った一時・退避ファイル |
| 実ゲーム | 対象コア・ゲームの起動結果(ROMを添付しない) |

## English

The recorded local run passed 81 tests on Windows with Python 3.11.9, PyQt6 6.11.0, and keyring 25.7.0 on 2026-10-03. Tests use synthetic data, temporary folders, offscreen GUI rendering, and mocked FTP. They do not certify real hardware behavior, power-loss recovery, concurrent use, or every supported environment. The Actions matrix is configured, but its results must be checked after pushing. Real MiSTer test results are not recorded here yet.
