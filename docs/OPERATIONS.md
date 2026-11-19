# 运行维护

主服务端口与Compose保持一致。数据保存在records命名卷，应用代码不需要写入权限。

```sh
docker compose logs --tail=80 app
docker compose restart app
docker compose exec -T app pytest -q
docker compose down --remove-orphans
```

健康检查GET /health。失败输入返回原因码，结构性错误422，过大文件413，陈旧复核409。
文件内容最大4MB。批次先计算再原子保存，失败不留半条记录。证据CSV按原始行号定位。

定等应付（M8，/grading）使用同一 DATA_DIR 下独立的 grading.sqlite：车结论与事件只追加，
已开应付锁单不可改写。容量不足返回 BIN_FULL，锁单车改化验返回409 PAYABLE_LOCKED。
该模块不做跨粮种混仓、税费与付款；删除 records 卷会同时清空批处理与定等两套记录。
