# 既知の制限 / Known limitations

- **入力形式・MD5**: 7z取り込み、CHD・DLV映像の処理は対象外です。interleave / patchを含むMRAは組み立てMD5を検証できませんが、必要部品が揃えば生成・送信対象になります。実際にMD5不一致を確認したMRAは除外します。ZIP検証はゲームの実機動作確認ではありません。
- **名前指定とCRC指定の共有**: 複数MRAが同じ部品名・SHA-1を名前検索とCRC検索で共有すると、送信用記録の統合順によって名前検索の条件が失われる場合があります。別名で同じ内容が入った送信先ZIPを、必要部品が揃っていると判定する可能性があります。修正予定です。
- **地域分類**: 分類はファイル名とMRAの内容からの推定です。ファイル名の英語圏・その他地域の表記が、MRA内部の日本表記より優先される場合があります。代替MRAの判定レポートを確認してください。
- **GUIとCLIのMRA変更確認**: GUIのSendは対象MRAの実ファイルも確認します。CLIのsendはDB上のカタログのみ照合するため、MRA変更後は必ずRunをやり直してください。
- **入力ZIPの差分判定**: サイズと更新時刻を使います。両方を維持した内容変更は自動検出できません。プールのパックが欠損していても、元ZIPが未変更なら通常Runでは取り込みを省略します。verify-poolで確認し、必要ならCLIのingestで元ZIPを再取り込みしてください。存在するが破損したパックの自動修復は行いません。
- **同時実行**: 同じ作業フォルダーでGUIとCLI、または複数プロセスを同時に実行しないでください。プロセスをまたぐ作業全体のロックはありません。
- **作業場所**: GUIはソース配置先の.local-workを使用します。インストールされたarcadeguiコマンドでは配置先がPythonのパッケージ領域になり、書き込み権限が足りない場合があります。通常はREADMEのソース起動手順を使用してください。CLIは実行時のフォルダー基準で、--workで変更できます。
- **プール容量**: 使わなくなったROM実体や古いパックの自動整理・統合はありません。長期利用で容量が増えます。プールはROM実体の保存先なので、任意のファイルを削除しないでください。
- **CLIの終了コード**: 一括処理の個別エラーや送信停止が結果JSONへ記録され、終了コード0になる場合があります。自動処理では終了コードだけでなく結果も確認してください。
- **FTP・キャッシュ・復旧**: FTP通信は暗号化されません。送信キャッシュはサイズと更新時刻が変わらないことを前提にします。内容変更でも両方が同じ場合、変更を見逃す可能性があります。通信断や強制終了では一時ファイル・退避ファイルが残り、旧ZIPへの復旧に失敗する場合があります。
- **不適合ZIPの削除**: 有効な送信用記録に対して不適合なローカルZIPがある場合、Sendはローカルと送信先の同名ZIPの削除を試みます。バックアップを作らないため、詳細はREADMEのSendの動作を確認してください。

## English

This tool does not process 7z inputs or CHD/DLV media. MD5 assembly is unsupported for interleave/patch; those layouts remain eligible when their parts are resolved, while confirmed MD5 mismatches are excluded. ZIP checks do not prove a game runs correctly.

Mixed name/CRC requirements can lose a name constraint when merged, and region classification is heuristic. GUI Send checks source MRA files; CLI Send checks only the database catalog. Run again after MRA changes. Unchanged source ZIPs are skipped even if their pool pack is missing; use verify-pool and explicit ingest as needed. Existing corrupt packs are not repaired automatically.

Do not run multiple processes against one work folder. Start the GUI from the repository as documented; installed entry points can use a package directory without write access. Pool garbage collection is unavailable. CLI exit code zero does not necessarily mean every item succeeded. FTP is unencrypted, caching trusts size/timestamp stability, and recovery is best-effort. Send can delete invalid local and remote ZIPs without a backup; read the README before use.
