# MiSTer Arcade Auto Setup / MiSTer Arcade 自動セットアップ

MRAに基づくMiSTerアーケード用ROM ZIP再構成ツールです。Python 3.10以上で動作し、CLI本体は標準ライブラリのみを使います。ROMはSHA-1でプール化し、元ZIP・元メンバー名をSQLiteに残します。原本ZIPやMRAは読み取り専用です。ROM・MRA・MiSTerの接続情報はリポジトリに含めません。

## GUIで使う

GUIにはPyQt6とkeyringが必要です。リポジトリのフォルダーで`python -m pip install ".[gui]"`を実行して導入できます。

```powershell
python arcadegui.py
```

Windowsでは`Start_GUI.cmd`をダブルクリックしても起動できます。

実行中の`Cancel`は安全な区切りでRunまたはSendを中止します。ZIP生成中は一時ファイルを片付け、完成済みのZIPは残します。FTP送信中は一時名のファイルを削除して中止を試みます。中止したRunの後は、送信前にRunを再実行してください。通信中は現在のFTP操作が戻るまで少し待つ場合があります。

画面右上の「言語 / Language」で日本語とEnglishを切り替えられます。選んだ言語は次回起動時にも使われます。

English quick start: install the GUI with `python -m pip install ".[gui]"`, then run `python arcadegui.py`. Select **English** at the top right. Choose an MRA folder and a source ROM ZIP folder, then use **Run** to build ZIPs. Enter your MiSTer IP address to fetch MRAs by FTP or to use **Send**. The language choice is saved for the next launch.

画面にMiSTerのIPアドレス・ユーザーID・パスワード、MRAのローカル保存先、元ROMフォルダーを入力します。接続先を空欄にすると、保存済みMRAを使ってローカルで処理します。`Run`でMRA取得からZIP生成まで実行し、`Send`で生成ZIPをMiSTerへ送信します。実行中は照合中のMRA、取り込み中のROM ZIP、作成・検証中の出力ZIPを状態欄とログに表示し、件数の分かる段階ではプログレスバーも更新します。完了後の`run-log.txt`にも処理の詳細を残します。画面左に設定と操作、右に「実行ログ」「今回追加したZIP」「未生成ZIP」「代替MRAの判定」のタブを配置します。IPアドレス・ユーザーID・フォルダーは次回起動時も保持します。パスワードは設定ファイルに書かず、OSの資格情報ストアに保存します。画面では`_alternatives`を「地域違い：日本版と判定」「地域違い：その他」「別バージョン」「改造版・非公式版」に分けて選べます。初期設定は日本版と判定されたものだけです。標準MRAは常に対象です。送信先は`/media/fat/games/mame`です。

代替版の分類にはMRAファイル名、MRA内の地域表記、セット名と親セット名、ROM ZIPの候補を使います。`Japan`、`Japanese`、`JP`、`日本`、`(J)`、`version J`などの表記を日本版として扱います。`klaxj3.zip|klax.zip`のように先頭のZIP名が親のZIP名に`j`と数字を付けた形、または`Bagman (Taito).mra`のようにMRAの`setname`が`parent`に`j`を付けた形で、そのZIPが候補に含まれる場合も日本版と判定します。ZIP名に単に`j`があるだけでは判定しません。`bootleg`、`hack`、`patch`などは改造版を優先します。日本版でも画面内の言語が日本語とは限りません。分類できない代替MRAはGUIでは対象外です。実行後、GUIの「代替MRAの判定」タブと`.local-work\alternative-selection-report.json`で分類と選択結果を確認できます。

## ROM ZIPを作成する（通常操作）

MRAフォルダーとROM ZIPフォルダーを用意し、このディレクトリで指定して実行します。以下の`local-data`は手元に作る作業用フォルダーの例で、Gitの対象外です。MiSTerからFTPでMRAを取得する場合は、指定したMRAフォルダーへ保存されます。ROMの入力フォルダーは複数指定できます。`--work`でDB、プール、出力の保存先を指定できます（既定は`.local-work`）。

```powershell
python arcadetool.py run .\local-data\mra .\local-data\rom-zips
python arcadetool.py run .\local-data\mra .\local-data\rom-zips --japan-alternatives
python arcadetool.py run .\local-data\mra .\local-data\rom-zips --include-alternatives
python arcadetool.py run .\local-data\mra .\local-data\rom-zips --mame-dat .\local-data\mame.dat
```

GUIの「MAME DAT」欄で`.dat`またはMAMEの`-listxml`ファイルを選べます。パスは次回起動時も保持します。指定すると`isbios="yes"`のセット名を読み取り、そのZIPだけを共有BIOSとして統合します。MRAが要求する部品に不足や内容競合があれば統合せず、原因を実行ログに残します。DATを指定しない場合はBIOSの自動判定と統合を行いません。MRAとDATのバージョンが異なる場合、名前が一致しないセットを推測でBIOS扱いしません。

MiSTerからMRAを取得する場合は、`config.example.json`をコピーし、`mister.host`にIPアドレス、`mister.username`にユーザー名を指定します。パスワードは`mister.password_env`が指す環境変数に設定します。MRAのFTP取得元は既定で`/media/fat/_Arcade`です。実機の配置が異なる場合は`mister.mra_root`を変更します。

```powershell
python arcadetool.py --config config.json run .\local-data\mra .\local-data\rom-zips
python arcadetool.py --config config.json send --dry-run
python arcadetool.py --config config.json send
```

`run`はFTPから変更されたMRAの取得、MRA解析、ZIP内のROM登録、MRAとの照合、完成可能なタイトルのZIP生成・検証を順に行います。CLIでは標準MRAのみが初期設定です。`--japan-alternatives`、`--other-region-alternatives`、`--version-alternatives`、`--modified-alternatives`を組み合わせられます。`--include-alternatives`は従来どおり分類不能なものも含む全代替MRAです。FTP未設定・接続不可の場合はMRA取得を飛ばし、保存済みMRAで処理を続けます。保存済みMRAもない場合は停止します。`send`は別操作で、出力ZIPをMiSTerへ送ります。送信前に、最新の`run`で記録したMRA要求と`out`の各ZIPの部品名・SHA-1・CRC32・サイズを再照合します。MRA変更後に`run`していない場合や、要求と一致しないZIPは`out`に残して送信を保留します。同名の転送先ZIPはSHA-1まで照合し、一致すれば再送を省略します。内容が異なるZIPは上書きせず保留し、ほかのZIPの送信は続けます。検証済みの送信先ZIPのSHA-1・サイズ・FTP更新時刻は`.local-work/send-cache.json`に記録します。次回Sendでこの3項目が一致すればMiSTerからZIP全体を読み戻さず、変更があれば再照合します。FTPサーバーが`MDTM`に対応しない場合は従来どおり全体を読み戻して照合します。

ROMが一部足りないMRA(代替MRAなど)は、そのMRAだけを除外して、完成しているMRAのぶんでZIPを作って送ります。除外したMRAと不足ROM(部品名・CRC)はSendの結果に表示し、`send-manifest.json`の`excluded`にも記録します。後からRunで不足ROMがプールに加わると、ZIPが再生成され、次のSendで送信先の同名ZIPを置き換えます。置き換えは、現在のMRAと一致すると検証済みのZIPに限り、一時名でアップロードして検証したあと、旧ZIPを`.replaced`へ退避して入れ替えます。失敗時は旧ZIPへ戻します。

ROM照合では取り込み済みZIPの出所情報から検索索引を一度だけ作り、MRAのZIP候補名とCRC、または部品名で検索します。各部品のたびにプール全体を走査しません。不足一覧に必要なため、未完成のMRAでも残りの部品を確認します。初回取り込みではZIPを展開してSHA-1とCRC32を計算し、ROMの実体をプールへ書き込みます。新規ROMは入力ZIPごとに1つのパックZIP(`.local-work\\pool\\packs\\<id>.zip`)へ、SHA-1名のメンバーとして圧縮保存します。各パックには`manifest.json`(収録ROMと出所)が入り、DBが失われても`rebuild-index`で復元できます。旧形式のばらファイル(`pool\\xx\\<sha1>`)も読み込めます。DBへの登録は部品ごとではなくZIP単位で確定します。中止時は処理中のZIPのDB更新を戻し、完成済みのZIPの記録を保持します。

生成ZIPは`.local-work\out`、不足ROMレポートは`.local-work\missing-report.json`、未登録ZIP候補一覧は`.local-work\missing-zips.txt`に保存します。`A.zip|B.zip`のような候補は左から検索しますが、出力先は実際に部品が見つかった入力ZIP名です。BIOS候補の後にゲームZIPが指定され、部品がゲームZIPから見つかった場合はゲームZIPへ出力します。`.local-work\missing-out-zips.txt`には、部品の出所に基づく出力ZIP名のうち未作成の名前を記録します。`.local-work\missing-out-causes.txt`には未生成ZIPごとに、ROM部品不足、照合の曖昧さ、生成エラーなどの原因と該当MRAをRunごとに記録します。既存の通常ROM ZIPは、必要な各部品がCRCと内容で含まれるかを調べ、余分な部品を許容します。DATでBIOSと判定したZIPはBIOS部品の構成と照合し、余分な部品があればバックアップ後に再構成します。同じ名前でもMRAが異なるCRCを要求するROMは、ZIP内に各CRCの部品を収録できます。`.local-work\run-log.txt`にも未生成ZIPの原因、新規生成ZIP名、既存ZIPの再利用数を記録します。変更のない入力ZIPは再展開せず、既存の生成ZIPは必要な部品をすべて含む場合に再利用します。ZIPに不足や不一致がある場合は結果の`rejected`または`errors`を確認してください。

DATでBIOSと判定したZIPも、実際にその入力ZIPから読み出したBIOS部品だけを集約します。BIOS候補を含む検索指定だけを理由にゲームROMをBIOS ZIPへ入れることはありません。全出力ZIPは選択中のMRAが要求する部品の和集合として作り、CRCが異なる同名部品も保持します。MRAがCRCを指定しない場合、同名で内容が異なる部品は曖昧として扱います。置き換える既存ZIPは`.local-work\backups`に保存します。

## 個別操作・調査

コードは処理単位で分けています。`arcadetool.py`はCLIと一括実行、`arcade_mra.py`はMRA解析とFTP取得、`arcade_pool.py`はROMの登録と照合、`arcade_build.py`はZIP生成と検証、`arcade_reports.py`は不足レポート、`arcade_transfer.py`はFTP送信を担当します。GUIは`arcadegui.py`が動作を制御し、`gui_layout.py`が画面配置、`gui_i18n.py`が日本語・英語の表示文言を担当します。

次のコマンドは途中段階の確認や再実行に使えます。

```powershell
python arcadetool.py parse-mra .\local-data\mra
python arcadetool.py ingest .\local-data\rom-zips
python arcadetool.py list --filter 'Astra SuperStars'
python arcadetool.py missing --filter 'Air Duel'
python arcadetool.py missing --zip --output .local-work\missing-zips.txt
python arcadetool.py missing --report .local-work\missing-report.json --zip --output .local-work\missing-zips.txt
python arcadetool.py build 'Astra SuperStars (J 980514 V1.002).mra'
python arcadetool.py build
python arcadetool.py verify .local-work\out\astrass.zip
python arcadetool.py pack-pool --dry-run
python arcadetool.py pack-pool
python arcadetool.py verify-pool
python arcadetool.py rebuild-index
python arcadetool.py --config config.json transfer --dry-run
python -m unittest discover -s tests -v
```

`config.example.json`の転送先は標準的な例です。実機のUSB、ネットワーク、SDカードの優先パスを確認してから`rom_root`などを設定してください。接続はFTPを使い、パスワードは設定の`password_env`で指定した環境変数から読みます。実転送は`transfer`です。接続設定がある`--dry-run`は宛先の衝突を照会します。実転送では同名ファイルが存在すれば停止し、一時名でアップロードしてサイズとSHA-1を再読込照合した後にリネームします。7z投入は未対応です。

`list`の`plans`に、構成ごとの不足、曖昧なSHA-1候補、選択した出所を出します。CRCがあるpartはCRCを優先し、ZIP候補を左から順に調べます。CRCがないpartは名前だけの低信頼候補です。複数SHA-1が残る場合は自動選択しません。`rom index="0"`の複数記述は代替構成です。

`missing`は不足ROMの名前・期待CRC・探索先ZIPを読みやすく表示します。ZIPごとに「プールに未登録」か「登録済み・該当ファイルなし」かを示します。`A.zip|B.zip`は左から探す候補であり、両方が必須という意味ではありません。全MRAを対象にする場合は`--filter`を省略します。機械処理用のJSONは`missing --json`で出せます。ZIPの登録状態は取り込んだファイルの記録に基づきます。

`missing --zip`は、プールに未登録のZIP候補名だけを重複なしで1行ずつ表示します。`--output`を付けるとUTF-8のテキストファイルへ保存します。既存の`missing-report.json`から作る場合は`--report`を指定します。`--filter`も併用できます。登録済みのZIPに該当ROMだけが欠けている場合、そのZIPは一覧に含めません。`|`でつながる名前は代替検索先なので、一覧に並んでもすべてを揃える必要があるわけではありません。

`build`にMRA名を指定しない場合は、必要なROM構成がすべてそろったタイトルをまとめてビルドします。未完成のタイトルは飛ばします。既存の出力ZIPは部品名・SHA-1・CRC・サイズを照合して一致すれば再利用し、異なる場合はそのタイトルを`errors`に記録して上書きしません。MRA名を指定した従来の`build`は、既存ZIPがあれば停止します。まとめてビルドした結果の`built`が新規生成ZIP、`already_present`が再利用したZIPです。

実値のMRA MD5は、単純な連結・埋め込み・repeat・offset・lengthを再現できる場合に照合します。interleaveまたはpatchを含む構成は`unsupported-interleave-or-patch`と記録し、完全検証扱いにしません。`md5="none"`と空値は`disabled`です。LaserdiscGamesのDLV映像は対象外です。生成したROM ZIPの部品検証はゲーム全体の動作確認を意味しません。

## 公開時の注意

ローカルの`config.json`、`local-data/`、`.local-work/`、ROM・MRA・CHDなどは`.gitignore`で除外しています。公開前には`git status --short`と`git ls-files`で、意図しないファイルが含まれていないことを確認してください。FTPは通信を暗号化しないため、信頼できるローカルネットワークで使用してください。

## プールのパック化(既存プールの移行)

`pack-pool`は旧形式のばらファイルをパックZIPへ移します。パックごとに「ハッシュ確認→パック書き込み→パックを読み直して検証→DB更新」を済ませてから、ばらファイルを削除します。壊れた・欠けたばらファイルはパックに入れず、削除せず報告します。`--dry-run`は計画のみ、`--keep-loose`はばらファイルを残します。`verify-pool`はDB上の全ROMを読み戻して確認します(`--quick`はSHA-1再計算を省略)。問題があれば内容を表示して終了コード1で終わります。`rebuild-index`はパックの`manifest.json`からblobs・origins・パック対応表を復元します。

推奨手順(Windows): ①`.local-work\arcade.db`を別の場所へコピーしてバックアップ、②`pack-pool --dry-run`で件数確認、③`pack-pool`、④`verify-pool`、⑤`run`でZIP生成が従来どおり動くことを確認。中断しても、完了済みのパックは有効で、再実行すると残りから再開します。
