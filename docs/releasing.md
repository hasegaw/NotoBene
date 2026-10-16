# リリース手順

## 識別子と版番号

配布ファイルは `NotoBene-YYYYMMDD-noto-<Noto SHA先頭12桁>-rNN.zip`、Git タグは `YYYYMMDD-noto-<Noto SHA先頭12桁>-rNN` です。固定の製品名に続けて日付を置くため、ファイル名順で公開日順に並びます。同じ日の異なる Noto コミット同士には時刻順を定義しません。

- `YYYYMMDD` は配布日（日本時間）です。`latest-noto` では最初の実行開始日時から決め、リリース用コミットに保存します。`pinned` では手動実行時に明示します。同じリリースの再実行では変えません。
- Noto の SHA は `sources/noto.lock.json` から取得します。`latest-noto` は公式の最新コミットに固定した lock をリリース用コミットに作成します。入力フォームで任意の SHA を指定することはできません。
- `rNN` は同じ Noto コミットに対する配布改訂番号です。初回は `1`、変更したビルダー・依存関係・梱包で配布し直すときは `2`、`3` と増やします。ファイル名では `r01` のように2桁にします（最大99）。新しい Noto コミットでは `1` から始めます。
- フォントそのものの版番号は `sources/font-version.txt` にあります。TTF が変わる配布では、Noto や配布改訂番号とは別に `2.000` → `2.001` のように増やしてコミットします。フォント内部の name、固有識別子、head.fontRevision、ビルドレポートは同じ設定を参照します。

例：`20261016-noto-f8d157532fbf-r01` のタグは、このリポジトリのビルドに使用したコミットを指します。元の Noto のコミットは lock と release manifest に記録します。短縮 SHA は表示用で、完全な40桁の SHA が追跡の根拠です。

## 公開の安全条件

初期状態ではタグ・リリース用ブランチ・Release を作成しません。GitHub の **Settings → Secrets and variables → Actions → Variables** に次の Repository variables を明示的に設定します。

| 変数 | 値 | 効果 |
| --- | --- | --- |
| `RELEASE_REPOSITORY` | 公開先の正確な `owner/repo` | 一致するリポジトリで手動公開を許可 |
| `ENABLE_SCHEDULED_RELEASES` | `true` | 上記条件に加え、週次の自動公開を許可 |

次の条件も必須です。

- `main` から実行します。定期実行にも `main` を既定ブランチとして設定してください。
- 変数が未設定、公開先が不一致、GitHub API のリポジトリ名が確認できない場合は停止します。コピー先で元の `RELEASE_REPOSITORY` を設定しても名前が一致しないため公開できません。
- workflow は書込権限を持つジョブの前に条件を確認し、準備・公開スクリプトも設定と GitHub API のリポジトリ情報を再確認します。

fork でも、`RELEASE_REPOSITORY` に fork 先の自分の正確な `owner/repo` を設定して初めて、その fork の Release を公開できます。GitHub の fork 判定や fork 情報の有無は公開条件にしません。

無効な定期実行は公開処理をスキップし、無効な手動実行は設定方法を表示して失敗します。通常の push・pull request のテストとビルドは、fork でも実行できます。

## 初回設定と公開

1. GitHub にリポジトリを用意し、このリポジトリの `main` を push します。Actions を有効にし、通常の **Build Noto Bene font** が成功することを確認します。
2. 上記の `RELEASE_REPOSITORY` を設定します。推奨設定として **Immutable releases** を有効にします。組織のポリシーや ruleset がある場合、workflow の `GITHUB_TOKEN` に `codex/noto-release/` 以下のブランチ、配布タグ、Release の作成を許可します。
3. Actions → **Release Noto Bene font** → **Run workflow** を開き、ブランチ `main` とモード `latest-noto` を指定します。公開日・改訂番号は入力不要です。未配布の Noto 入力があれば、検証成功後に Release を自動公開します。
4. 完了後、Release の ZIP と `SHA256SUMS` を確認します。毎週の更新確認も行う場合は `ENABLE_SCHEDULED_RELEASES=true` を設定します。通常は追加の PAT や Secret は不要です。

公開 workflow はテスト・Noto 取得・ビルド・独立検証・比較画像生成を共通の build workflow で実行します。その後 ZIP を作り、同じ実行の artifact を公開ジョブへ渡します。リリース用コミットを準備するジョブと公開ジョブだけが `contents: write` を持ちます。公開はコミット確認 → タグ作成 → Draft 作成 → 全アセット添付・ハッシュ照合 → 公開の順です。タグ push による別 workflow の起動には依存しません。

全リリースを同じ concurrency group で直列実行し、最大100件の待機を保持します。ビルドとタグは準備ジョブが確定したコミット SHA を使用します。実行中に `main` が進んでも対象は変わりません。

## 最新 Noto の手動・週次チェック

週次実行は毎週日曜 **09:00 JST**（UTC の日曜00:00、cron `0 0 * * 0`）です。GitHub の定期実行は混雑で遅れる場合があり、公開リポジトリでは長期間の無活動による自動無効化にも注意してください。[GitHub の schedule 仕様](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

`latest-noto` は公式 `notofonts/noto-cjk` の既定ブランチのコミットを確定し、その SHA から対象の JP Regular フォントとライセンスを取得します。フォントとライセンスの両方のハッシュ・サイズが既存の公開物と一致すれば、上流に README などの変更があっても新規公開しません。初回は、現在の lock と同じ内容であっても未配布なら公開対象になります。

新しい入力があれば、`codex/noto-release/<フォントSHA256>-<ライセンスSHA256>` ブランチへ、lock・OFL・フォント版番号・配布日を保存するコミットを作ります。このコミットをチェックアウトして検証し、成功した場合だけ配布タグと Release を作成します。`main` は変更しません。専用ブランチは途中失敗からの再開にも使うため保持します。同じ入力のブランチがすでにあれば、そのコミットと日付を再利用します。

初回のフォント版番号は現在の設定を使い、以後の入力更新では現在の設定と公開済みの版番号の最大値から `0.001` 増やします。最新 Noto の自動配布は新しい入力につき `r01` です。ビルダーだけの修正は `pinned` モードで配布してください。

## lock を固定した修正版

`pinned` モードは `main` にコミットされた `sources/noto.lock.json` とフォント版番号を使います。対象の lock・加工処理・依存関係・版番号を更新してコミットし、手動実行時に `mode=pinned`、公開日（例：`20261020`）、同じ Noto に対する次の改訂番号（例：`2`）を指定します。

## 配布内容

Release に添付するファイル：

- `NotoBene-YYYYMMDD-noto-<SHA>-rNN.zip`
- `SHA256SUMS` — ZIP の SHA-256
- `release-manifest.json` — ZIP とは別に添付する来歴情報

ZIP は同名の最上位フォルダーを持ち、その中にフォントとライセンス類の3ファイルだけを収録します。

```text
NotoBene-Regular.ttf
OFL.txt
FONT-NOTICE.txt
```

別添の manifest は両リポジトリの完全なコミット SHA、Noto 入力のハッシュとサイズ、フォントの版番号、配布改訂番号、Python・依存ライブラリ・zlib の版、ZIP に収録した3ファイルのハッシュとサイズを記録します。公開前に ZIP の収録内容とハッシュを検証し、Noto の来歴とフォント版番号をビルドに使用したコミットの設定と照合します。`noto.lock.json`、`build-report.json`、`validation.json` はビルド・梱包時の入力検証に引き続き使用し、比較画像とともに Actions の `noto-bene` artifact（30日保存）へ保存します。JSON ファイルは配布 ZIP に含めません。

展開後の TTF をインストールしてください。再配布する場合は OFL と FONT-NOTICE を同梱します。ZIP の確認例：

```sh
sha256sum -c SHA256SUMS
# macOS
shasum -a 256 -c SHA256SUMS
```

ZIP の収録順・ファイル権限・日時を固定し、同一のビルド環境・入力・ビルダーコミット・配布日・改訂番号で同一バイト列を生成します。実行 ID や実行時刻は ZIP に入れません。Python と圧縮ライブラリの異なる環境までバイト一致を保証するものではありません。

## 再実行と修正版

失敗時は原則として同じ workflow run を再実行してください。日付やコミットを変えて新規実行すると別の入力になります。

- Draft の一部アセットまで作成済みの場合、既存タグのコミットとアセットのハッシュが一致するときだけ不足分を添付して公開します。
- 公開済みで全アセットが一致するときは、何も変更せず成功します。
- タグのコミット、アセットの内容、ファイル数などが不一致の場合は停止します。タグの移動、アセットの削除や上書きは行いません。
- 同じ Noto と改訂番号を別の日付で登録しようとした場合も停止します。訂正配布では新しい改訂番号を使用してください。
- 既存の公開済みリリースより古い日付の配布は Latest にしません。

同じコードでもホストイメージや Python のパッチ版が変わると、ツールチェーン情報や ZIP の圧縮結果が変わる場合があります。公開だけの失敗なら、Actions の **Re-run failed jobs** で成功済みビルドの artifact を再利用できます。再ビルドして既存アセットと不一致になった場合は、新しい配布改訂番号で公開してください。

## ローカルで梱包を確認

依存関係を導入し、コミット済みのソースで次を実行します。`OWNER/REPO` は実際の GitHub リポジトリに置き換えてください。ローカルの package コマンドは GitHub を変更しません。

```sh
python -m unittest discover -s tests -v
python scripts/build.py
python validation/verify_font.py \
  --font dist/NotoBene-Regular.ttf \
  --source vendor/noto/NotoSansCJKjp-Regular.otf \
  --json dist/validation.json
python scripts/release.py package \
  --release-date 20261016 --revision 1 \
  --repository OWNER/REPO --source-sha "$(git rev-parse HEAD)" \
  --dist-dir dist
```

結果は `dist/release/` に生成されます。package は lock・入力ハッシュ・出力ハッシュ・ライセンス・フォント版番号・独立検証の成功を確認します。通常の公開には Actions を利用し、ローカルで `publish` を実行する必要はありません。

手動で Noto を更新するときは `sources/noto.lock.json` の完全 SHA、URL、サイズ、SHA-256 と `sources/OFL.txt` を更新し、フォントの版番号を増やして通常の CI を通します。自動チェックはリリース用ブランチでこれを行い、lock 更新 PR は作成しません。
