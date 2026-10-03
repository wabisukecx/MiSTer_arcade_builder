# MiSTer Arcade Auto Setup / MiSTer Arcade 自動セットアップ

MRA(MiSTerのアーケード用定義ファイル)に基づいて、MiSTer用のROM ZIPを再構成して送るツールです。手元のROMをSHA-1でプールにまとめ、MRAが要求する部品だけを集めたZIPを作って、MiSTerへFTPで送ります。原本のROM ZIPとMRAは読み取り専用で、変更しません。ROM・MRA・MiSTerの接続情報は、リポジトリに含めません。

操作は基本的にGUIで行います。CLIは調査・保守用です(最後の節)。

## 準備と起動

Python 3.10以上が必要です。GUIにはPyQt6とkeyringを使います。リポジトリのフォルダーで次を実行して導入します。

```powershell
python -m pip install ".[gui]"
```

起動は、`Start_GUI.cmd`をダブルクリックするか、次を実行します。

```powershell
python arcadegui.py
```

English quick start: install with `python -m pip install ".[gui]"`, then run `python arcadegui.py`. Select **English** at the top right. Choose an MRA folder and a source ROM ZIP folder, then use **Build ROM ZIPs** to build ZIPs. Enter your MiSTer IP address to fetch MRAs by FTP or to use **Send to MiSTer**. The language choice is saved for the next launch.

## 使い方

画面左に設定と操作、右に「実行ログ」「今回追加したZIP」「未生成ZIP」「代替MRAの判定」「不足ROM」のタブを配置しています。「不足ROM」タブは、最新のRunでROMが足りなかったものを、ZIP → MRA → 不足部品(名前とCRC)の順に一覧します。ZIPは、「未生成」(足りないMRAしかなく作れない)と、「生成済み(下のMRAのみ除外)」(ほかのMRAのぶんで作成済み)に分かれます。画面右上の「言語 / Language」で日本語とEnglishを切り替えられます。

入力する項目は次の通りです。IPアドレス・ユーザーID・フォルダー・チェックボックスの状態は、次回起動時も保持されます。パスワードは設定ファイルに書かず、OSの資格情報ストアに保存します。

- **MiSTerのIPアドレス・ユーザーID・パスワード**: MRAの取得とSendに使います。空欄にすると、保存済みのMRAを使ってローカルだけで処理します。
- **MRAのローカル保存先**: FTPで取得したMRAもここに保存します。
- **元ROMフォルダー**: ROMのZIPがあるフォルダーです。
- **代替MRA**: `_alternatives`の代替MRAのうち、どれを対象にするかを選びます。標準MRAは常に対象です。
- **MAME DAT**: 任意です。後の節を参照してください。

操作は次の3つです。

- **ROM ZIPを作成**(以下、Run): MRAの取得、解析、ROMの取り込み、MRAとの照合、完成できるタイトルのZIP作成と検証を順に行います。実行中は、照合中のMRA、取り込み中のROM、作成・検証中のZIPを、状態欄とログに表示します。完了後に`run-log.txt`へ詳細を残します。
- **MiSTerへ送信**(以下、Send): 作成したZIPをMiSTerへ送ります。
- **中止**(Cancel): 安全な区切りでRunまたはSendを中止します。ZIP作成中は一時ファイルを片付け、完成済みのZIPは残します。FTP送信中は一時名のファイルを削除して中止を試みます。通信中は、現在のFTP操作が戻るまで少し待つ場合があります。中止したRunの後は、Sendの前にRunをやり直してください。

送信先は`/media/fat/games/mame`です(MRAは`/media/fat/_Arcade`から取得します)。

## 代替MRAの選択

`_alternatives`の代替MRAは、「地域違い」の2つと、「別バージョン」「改造版・非公式版」に分けて選べます。標準MRAは、常に対象です。初期設定は、日本語表示では「地域違い：日本版と判定されたもの」だけです。分類できない代替MRAは、GUIでは対象外です。

「地域違い」の2つは、表示言語に合わせて意味が変わります。

- **日本語表示**: 1つ目が日本版、2つ目が「その他の地域」(英語圏を含む)です。
- **English表示**: 1つ目が英語圏版(US・World・Europe・UK・Australia)、2つ目が「その他の地域」(日本を含む)です。

言語を切り替えると、その言語で最後に選んでいた状態に切り替わります(言語ごとに別々に保存します)。選択が変わると、作られるZIPの内容も変わるので、切り替えた後は、設定を確認してからZIPを作成してください。

分類には、MRAのファイル名、MRA内の地域表記、セット名と親セット名、ROM ZIPの候補を使います。日本版は、`Japan`、`Japanese`、`JP`、`日本`、`(J)`、`version J`などの表記で判定します。`klaxj3.zip|klax.zip`のように、先頭のZIP名が親のZIP名に`j`と数字を付けた形のときも、日本版と判定します。`Bagman (Taito).mra`のように、`setname`が`parent`に`j`を付けた形で、そのZIPが候補に含まれるときも同様です。ZIP名に単に`j`があるだけでは判定しません。英語圏版は、`USA`、`US`、`World`、`Europe`、`UK`、`Australia`、`English`などの表記で判定します。日本版の表記を先に調べるので、`Japan`と`World`の両方を含む名前は、日本版です。`bootleg`、`hack`、`patch`などは、改造版を優先します。日本版でも、画面内の言語が日本語とは限りません。

Runの後に、「代替MRAの判定」タブと`.local-work\alternative-selection-report.json`で、分類と選択の結果を確認できます。

## MAME DAT(任意)

「MAME DAT」欄で、`.dat`またはMAMEの`-listxml`ファイルを選べます。パスは次回起動時も保持します。指定すると、`isbios="yes"`のセット名を読み取り、そのZIPだけを共有BIOSとして統合します。MRAが要求する部品に不足や内容の競合があるときは、統合せず、原因を実行ログに残します。DATを指定しないときは、BIOSの自動判定も統合も行いません。MRAとDATのバージョンが異なっても、名前が一致しないセットを推測でBIOS扱いすることはありません。

DATでBIOSと判定したZIPには、実際にその入力ZIPから読み出したBIOS部品だけを集めます。BIOS候補を含む検索指定だけを理由に、ゲームのROMをBIOS ZIPへ入れることはありません。

## 出力ZIPの圧縮

新しく作る出力ZIPは、標準で圧縮(deflate)します。作成済みのZIPは、そのまま再利用されるので、以前に作った無圧縮のZIPは、作り直すまで無圧縮のままです。作り直したいときは、`.local-work\out`の該当ZIPを退避してから「ROM ZIPを作成」を実行してください。ZIPのバイト列が変わっても、送信先のZIPに必要なROMが揃っていれば、置換送信にはなりません(Sendの動作を参照)。GUIに圧縮の設定はありません。CLIでは、`config.json`の`"output": {"compression": "stored"}`で、無圧縮にできます。

## Sendの動作

Sendは、MiSTerに送る前に、最新のRunで記録したMRAの要求と、`out`の各ZIPの部品名・SHA-1・CRC32・サイズを照合します。Runが途中で止まったり、MRAを変更した後にRunしていなかったりして、記録が古いとき(または無いとき)は、Send全体を中止します。このとき、MiSTerへの接続も、ZIPの削除も行いません。先にRunを実行してください。GUIのSendは、MRAフォルダーのファイルそのものも読み直し、Run後に追加・変更・削除があれば同じく中止します(`send`コマンドはDB上のカタログとだけ照合します)。記録は最新でも、個々のZIPが現在のMRAの要求と一致しないとき(どのMRAも要求しないZIPを含む)は、そのZIPだけを保留し、MiSTerと`out`から削除します。

- **同名のZIPが送信先にあるとき**: SHA-1まで照合し、一致すれば再送を省略します。送信先のZIPのSHA-1・サイズ・FTP更新時刻は`.local-work\send-cache.json`に記録します。次回のSendで、この3項目が一致すれば、ZIP全体を読み戻さず、変更があれば再照合します。FTPサーバーが`MDTM`に対応していないときは、毎回全体を読み戻して照合します。
- **内容は違うが、必要なROMが揃っているとき**: 送信先のZIPのバイト列が違っていても(圧縮の有無、メンバーの順序、余分なファイルなどの違い)、現在のMRAが要求するROMがすべて入っていれば、置き換えずに再送を省略します。確認のため、送信先のZIPを一度だけ読み戻して、各メンバーのCRC・SHA-1・サイズを調べます。結果は、送信先のZIPのSHA-1・サイズ・FTP更新時刻と、要求するROMの一覧の指紋を`send-cache.json`に記録します。次回のSendで、この記録と一致すれば、読み戻さずに省略します。MRAの要求が変わったときは、指紋が変わるので、再確認します。Sendの結果では、「再送省略(必要なROMは揃っている)」と表示されます。
- **内容が違うとき**: 現在のMRAと一致すると検証済みのZIPに限り、一時名でアップロードして検証した後、旧ZIPを`.replaced`へ退避して入れ替えます。失敗したときは、旧ZIPへ戻します。検証できないZIPは、上書きせず保留し、ほかのZIPの送信は続けます。
- **中断された送信の残りがあるとき**: 通信が切れたときに残った、`.uploading`(送信中の一時ファイル)と`.replaced`(置換前の退避)は、次のSendで自動的に片付けます。
- **ROMが足りないMRAがあるとき**: ROMが一部足りないMRA(代替MRAなど)は、そのMRAだけを除外して、完成しているMRAのぶんでZIPを作って送ります。除外したMRAと不足ROM(部品名・CRC)は、Sendの結果に表示し、`send-manifest.json`の`excluded`にも記録します。後からROMをプールへ取り込んでRunすると、ZIPが再生成され、次のSendで送信先の同名ZIPを置き換えます。

## 保存先と出力ファイル

作業ファイルは、すべて`.local-work`に保存します。

- `out`: 作成したROM ZIP。
- `arcade.db`、`pool`: 取り込んだROMのデータベースとプール。ROMの実体そのものなので、削除しないでください。
- `missing-report.json`: 不足ROMのレポート。
- `missing-zips.txt`: プールに未登録のZIP候補名の一覧。`A.zip|B.zip`のような名前は、左から探す候補で、両方が必須という意味ではありません。
- `missing-out-zips.txt`: 部品の出所に基づく出力ZIP名のうち、未作成のもの。
- `missing-out-causes.txt`: 未作成ZIPごとの原因(ROM不足、照合の曖昧さ、生成エラーなど)と該当MRA。
- `run-log.txt`: 未生成ZIPの原因、新規作成したZIP名、再利用したZIPの数。
- `send-manifest.json`、`send-cache.json`: Sendが使う記録。
- `backups`: 置き換える既存ZIPの退避先。

## ROMの取り込みと照合

初回の取り込みでは、ZIPを展開してSHA-1とCRC32を計算し、ROMの実体をプールへ書き込みます。変更のない入力ZIPは、再展開しません。安全のため、1つのZIPの展開後の合計が8GiBを超えるものと、メンバー数が20,000を超えるもの、1つのメンバーが1GiBを超えるものは、取り込まずに、実行結果の`rejected`に記録します。新規のROMは、入力ZIPごとに1つのパックZIP(`.local-work\pool\packs\<id>.zip`)へ、SHA-1名のメンバーとして圧縮保存します。各パックには`manifest.json`(収録ROMと出所)が入り、DBが失われても復元できます。DBへの登録は、ZIP単位で確定します。中止したときは、処理中のZIPのDB更新を戻し、完成済みのZIPの記録を保持します。

MRAとの照合では、MRAのZIP候補名とCRC、または部品名で探します。CRCがある部品はCRCを優先し、ZIP候補を左から順に調べます。CRCがない部品は、名前だけの低信頼な候補です。複数のSHA-1が残るときは、自動選択しません。同じ名前でも、MRAが異なるCRCを要求する部品は、ZIP内にCRCごとに収録できます。

出力ZIPは、選択中のMRAが要求する部品の和集合として作ります。既存の出力ZIPは、部品名・SHA-1・CRC・サイズが一致すれば再利用します。通常のROM ZIPには、余分な部品があっても許容します。DATでBIOSと判定したZIPは、BIOS部品の構成と照合し、余分な部品があれば、バックアップ後に再構成します。

MRAのMD5は、単純な連結・埋め込み・repeat・offset・lengthを再現できるときに照合します。interleaveまたはpatchを含む構成は、`unsupported-interleave-or-patch`と記録し、完全な検証扱いにしません。`md5="none"`と空値は`disabled`です。LaserdiscGamesのDLV映像と7zの取り込みは、対象外です。MD5が実際に不一致だったMRAは、ZIPの作成にも送信にも使わず、エラーと「MD5不一致」の除外として記録します(検証できない構成とは区別します)。生成したZIPの部品検証は、ゲーム全体の動作確認を意味しません。

## コードの構成

`arcadetool.py`はCLIと一括実行、`arcade_mra.py`はMRA解析とFTP取得、`arcade_pool.py`はROMの登録と照合、`arcade_store.py`はプールのパック保存・検証・復元、`arcade_build.py`はZIP作成と検証、`arcade_bios.py`はDATからのBIOS判定、`arcade_reports.py`は不足レポート、`arcade_transfer.py`はFTP送信、`arcade_common.py`は共通処理を担当します。GUIは、`arcadegui.py`が動作を制御し、`gui_layout.py`が画面配置、`gui_i18n.py`が日本語・英語の表示文言を担当します。テストは、`python -m unittest discover -s tests -v`で実行します。

## 公開時の注意

ローカルの`config.json`、`local-data/`、`.local-work/`、ROM・MRA・CHDなどは、`.gitignore`で除外しています。公開前には、`git status --short`と`git ls-files`で、意図しないファイルが含まれていないことを確認してください。FTPは通信を暗号化しないので、信頼できるローカルネットワークで使用してください。

## CLI(調査・保守用)

GUIの操作は、CLIでも実行できます。MiSTerへ接続するときは、`config.example.json`をコピーして`config.json`を作り、`mister.host`にIPアドレス、`mister.username`にユーザー名を指定します。パスワードは、`mister.password_env`が指す環境変数に設定します。出力ZIPの圧縮は、`"output": {"compression": "deflate"}`(既定)か`"stored"`(無圧縮)で選びます。`--work`でDB・プール・出力の保存先を変えられます(既定は`.local-work`)。以下の`local-data`は、手元に作る作業用フォルダーの例です。

```powershell
python arcadetool.py --config config.json run .\local-data\mra .\local-data\rom-zips
python arcadetool.py --config config.json send --dry-run
python arcadetool.py --config config.json send
```

`run`のCLIの既定は、標準MRAのみです。`--japan-alternatives`、`--english-alternatives`、`--other-region-alternatives`(英語圏も含む、日本以外のすべての地域)、`--version-alternatives`、`--modified-alternatives`を組み合わせられます。`--include-alternatives`は、分類できないものも含む、全代替MRAです。`--mame-dat`でDATを指定できます。FTPが未設定か接続できないときは、MRAの取得を飛ばして、保存済みMRAで処理を続けます。保存済みMRAもないときは、停止します。

途中段階の確認や、プールの保守には、次のコマンドを使います。

```powershell
python arcadetool.py parse-mra .\local-data\mra
python arcadetool.py ingest .\local-data\rom-zips
python arcadetool.py list --filter 'Astra SuperStars'
python arcadetool.py missing --filter 'Air Duel'
python arcadetool.py missing --zip --output .local-work\missing-zips.txt
python arcadetool.py missing --report .local-work\missing-report.json --zip --output .local-work\missing-zips.txt
python arcadetool.py build
python arcadetool.py build 'Astra SuperStars (J 980514 V1.002).mra'
python arcadetool.py verify .local-work\out\astrass.zip
python arcadetool.py pack-pool --dry-run
python arcadetool.py pack-pool
python arcadetool.py verify-pool
python arcadetool.py rebuild-index
python arcadetool.py --config config.json transfer --dry-run
```

- `list`: `plans`に、構成ごとの不足、曖昧なSHA-1候補、選択した出所を出します。`rom index="0"`の複数記述は、代替構成です。
- `missing`: 不足ROMの名前・期待CRC・探索先ZIPを表示します。ZIPごとに「プールに未登録」か「登録済み・該当ファイルなし」かを示します。`--json`で機械処理用のJSONを出せます。`--zip`は、プールに未登録のZIP候補名だけを、重複なしで表示します。`--output`でUTF-8のテキストに保存できます。
- `build`: MRA名を指定しないときは、必要なROM構成がそろったタイトルをまとめて作ります。未完成のタイトルは飛ばします。既存のZIPが一致すれば再利用し、異なるときは`errors`に記録して上書きしません。MRA名を指定した`build`は、既存のZIPがあれば停止します。
- `pack-pool`: 旧形式のばらファイルをパックZIPへ移します。パックごとに、「ハッシュ確認→パック書き込み→読み直して検証→DB更新」を済ませてから、ばらファイルを削除します。壊れた・欠けたばらファイルは、パックに入れず、削除せずに報告します。`--dry-run`は計画のみ、`--keep-loose`はばらファイルを残します。
- `verify-pool`: DB上の全ROMを読み戻して確認します。`--quick`はSHA-1の再計算を省略します。問題があれば、内容を表示して、終了コード1で終わります。
- `rebuild-index`: パックの`manifest.json`から、blobs・origins・パック対応表を復元します。
- `transfer`: `send`と同じMRAとの照合を行いますが、既存のファイルの置き換えと内容一致での再送省略は行わない、単純な転送です。通常はGUIの`Send`か`send`を使ってください。同名のファイルが送信先にあるときは停止し、一時名でアップロードして、サイズとSHA-1を読み戻して照合した後に、リネームします。

プールの移行は、次の手順で行います(Windows)。①`.local-work\arcade.db`を別の場所へコピーしてバックアップ、②`pack-pool --dry-run`で件数を確認、③`pack-pool`、④`verify-pool`、⑤`run`で、ZIPの作成が従来どおり動くことを確認。中断しても、完了済みのパックは有効で、再実行すると残りから再開します。

`config.example.json`の転送先は、標準的な例です。実機のUSB、ネットワーク、SDカードの優先パスを確認してから、`rom_root`などを設定してください。
