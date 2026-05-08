python -m src.utils.prepare_spider2_samples ^
  --source-jsonl D:\Workspace\ReFoRCE\spider2-snow\spider2-snow.jsonl ^
  --gold-sql-dir D:\Workspace\ReFoRCE\spider2-snow\evaluation_suite\gold\sql ^
  --output-json D:\Workspace\Ace\Knowdata\data\input_20.json ^
  --output-ground-truth-dir D:\Workspace\Ace\Knowdata\data\ground_truth ^
  --count 20 ^
  --seed 42 ^
  --clean-ground-truth-dir
