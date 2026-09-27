# イベント再試行の専用環境検証

2026-09-27、ユーザーの指示に基づき専用Worker `ie-event-bot-e2e` で実行した。本番Worker・本番の外部資源はテスト対象にしていない。

## 実行結果

| 対象 | workflow | 結果 |
| --- | --- | --- |
| Discord通知の後続処理と復旧 | [36318327731](https://github.com/lycanthr0pes/IE_Event_Bot_fork/actions/runs/36318327731) | 成功・回収済み |
| 再試行の待機・隔離・再投入、状態障害10ケース | [36318551527](https://github.com/lycanthr0pes/IE_Event_Bot_fork/actions/runs/36318551527) | 成功・回収済み |
| Google matrixの準備 | [36318671481](https://github.com/lycanthr0pes/IE_Event_Bot_fork/actions/runs/36318671481) | 削除履歴100件上限で作成前に停止 |
| Google同期のNotion書戻し失敗後の復旧 | [36319179299](https://github.com/lycanthr0pes/IE_Event_Bot_fork/actions/runs/36319179299) | 成功・回収済み |

成功した実行についてGitHub Environment `e2e` の承認記録、workflow成功、実行commitとclean checkout、run ID・Worker version・deployの一致、JUnit、監査、`outcome=passed`、全manifestの`dirty=false`を回収artifactから独立照合した。

Google matrixの実行は`google_sync_baseline_limit`で新規manifest・外部資源の作成前に停止した。回収要求8回は前回runのclean manifestに対し`google_sync_run_mismatch`で拒否され、既存記録を保持した。最終artifactの全manifestは`dirty=false`であり、今回の新規資源はない。この実行を成功や`failed_clean`とは扱わない。削除履歴を消さず、256件の履歴に対応する既存の書戻し復旧モードへ切り替えた。

通常処理17ファイル（E2E専用モジュールを除くPython・本番設定・依存定義）は、本番バージョン`65e5be1a-dd44-42ff-881a-a1a2ed25adc0`へデプロイした指紋と一致する。通知試験のcommitは`d284083ab7602ccde9b2494225e73fdf46789002`、状態試験とGoogle書戻し復旧試験は`e7bf5d12e579453db52f6bfe81ccb405a77cba3a`である。後者の追加変更はE2E用検証と文書に限る。本番のデプロイ一覧も読戻し、上記バージョンの100%適用が維持されていることを確認した。

## 確認した動作

- **通知復旧**: 実Discordイベント2件を実Notionへ反映し、実Discordチャンネルへ2件通知した。先行イベントのリアクション処理に固定失敗を注入し、後続イベントの通知が先に進むこと、先行イベントが同じmessage IDのリアクション再試行だけで復旧することを確認した。イベント・通知・Notionページ・所有KVを回収した。監査18行・9操作、JUnit 1,136件。
- **既定300秒の待機**: 失敗直後の通常差分処理で、再試行を追加せず後続イベントを処理することを確認した。300秒を実際に待ち切る試験ではなく、期限前の抑止の検証である。期限直前・期限到達の境界はローカル単体テストで確認している。
- **上限と隔離**: 間隔だけ1秒に短縮し、通常差分処理で初回を含め6回失敗させた。隔離後の再呼出しが7回目の自動処理を行わないことを確認した。
- **管理ハンドラと再投入**: Worker内から通常の`Application.fetch`を呼び、認証拒否、隔離一覧、実DOロック下の再投入、同じ再投入要求の再送がカウンターを再初期化しないことを確認した。投稿済みIDを保持して復旧し、queueが空になることを検査した。管理ハンドラの保存先はrun所有KV prefixに限定している。
- **保存と回収**: 状態試験では実KVへの保存、別HTTPのverifyによる状態・証拠hashの読戻し、所有KVの回収、制御DOのロック解放を確認した。監査28行・14操作、JUnit 1,138件。
- **Google同期の部分失敗復旧**: 所有Google予定3件を使い、実NotionのDiscord ID書戻し要求に不正入力を注入した。APIの400・通常dispatchの500、cursor維持、保存queueだけの再試行、既存ID再利用、全件再適用後の重複なしを4段階と各verifyで確認した。Google予定3件・Discord予定3件・Notionページ3件・共有KV6キーを回収した。監査22行・11操作、JUnit 1,138件。詳細な試験契約は[書戻し復旧](E2E-NOTION-WRITEBACK-RETRY.md)を参照。

状態試験の通知と失敗は固定モデルであり、外部APIは呼ばない。Google・Discord・Notionの自然発生障害、全地域KVの整合、厳密な一度限りの配信、外部HTTPクライアントから管理APIまでの経路、開催中イベントと翌日イベントの実データの組合せは、この追加試験では実証していない。

## 証跡

ローカルの`test-results/retry-e2e-<workflow ID>/`へartifact・workflow状態・承認記録を保存し、`verification.json`へ独立照合結果を記録した。GitHub artifactは既存workflowの保持期間に従う。

実行ブランチは`feature/sync-retry-e2e-20260927`。元の作業ツリーの未コミット変更は保持した。追加検証後のローカル結果はPython 1,138件・Node 381件成功、Ruff・Pyright・E2E設定検査・E2E dry-run成功。

運用仕様は[イベント同期の再試行](SYNC-RETRY.md)、実行契約は[テスト](TESTING.md)を参照。
