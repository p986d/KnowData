@echo off

@REM 随机抽 10 个，写到默认 data/input.json，并复制 SQL 到默认 data/ground_truth
python -m src.utils.prepare_spider2_samples --count 10

@REM 指定 instance_id 列表
@REM py D:\Workspace\Spider2Test\src\utils\prepare_spider2_samples.py --instance-ids sf002 sf011 sf_bq028

@REM 从文件读取 instance_id，每行一个
@REM py D:\Workspace\Spider2Test\src\utils\prepare_spider2_samples.py --instance-id-file D:\Workspace\ids.txt

@REM 复制前先清空目标目录下已有的 .sql
@REM py D:\Workspace\Spider2Test\src\utils\prepare_spider2_samples.py --count 10 --clean-ground-truth-dir
