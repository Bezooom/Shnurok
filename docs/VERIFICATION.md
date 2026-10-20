# Верификация протокола ШНУРОК

## Стенд тестирования

| Параметр | Значение |
|---|---|
| ОС хоста | Ubuntu 24.04 LTS, ядро Linux 7.0.0-38-generic x86_64 |
| Хост-контроллер USB | ASMedia Technology Inc. ASM1142 USB 3.1 Host Controller (`1b21:1242`) |
| Подключение | USB-C порт хоста → Lightning-кабель Apple |
| Целевое устройство | iPad Air 1 (Wi-Fi, `iPad4,1`, SoC Apple A7 S5L8960X, BDID 0x10) |
| Версия прошивки | iOS 12.5.7 (Build 16H81) |
| Среда рантайма | Python 3.12, libusb 1.0.27 (ctypes backend) |

---

## Контрактные тесты протокола ep0

Тестовый стенд `tools/test_ep0_protocol.py` валидирует взаимодействие хостового загрузчика и обработчика `ep0_upload_chunk` в pongoOS:

```bash
make test
```

### Лог прогона

```text
PASS  data lands while bulk is pending and never completes
PASS  chunks arrive in order with sequential wValue
PASS  finalize commands reach the shell
PASS  resize path not swallowed by chunk handler
PASS  4-byte chunk with nonzero wValue is data, not resize
PASS  oversized chunk rejected
PASS  overflow sets errManifest
PASS  GETSTATUS readable
PASS  upload completes when GETSTATUS stalls
```

### Матрица приёмки

| Тест / Инвариант | Условие и проверяемый контракт | Результат | Вердикт |
|---|---|---|---|
| **Pending bulk bypass** | Буфер наполняется через ep0, даже если Bulk OUT `0x02` взведён и никогда не завершается | `loader_xfer_recv_count == 4096`, `waiting_bulk == True` | PASS |
| **Sequential chunk order** | Чанки принимаются строго по монотонно возрастающему индексу `wValue` | Порядок 1..N проверен, рассинхронизация отклоняется | PASS |
| **Command pipe passthrough** | Завершающие shell-команды передаются через `0x21/3` в буфер команд pongoOS | Команда `boot` попадает в shell dispatch | PASS |
| **Resize collision prevention** | Запрос ресайза (`0x21/1`, `wValue=0`, `wLength=4`) не перехватывается обработчиком данных | Буфер расширяется, флаг активного аплоада не сбрасывается | PASS |
| **4-byte chunk disambiguation** | 4-байтовый чанк данных с `wValue > 0` корректно классифицируется как данные, а не ресайз | 4 байта добавлены в буфер, размер не изменён | PASS |
| **DMA chunk boundary check** | Чанк размером > `0x1000` (4096 байт) отклоняется со STALL | `ValueError / Stall` при превышении буфера DMA | PASS |
| **Buffer overflow protection** | Попытка переполнения выделенного буфера выставляет `bStatus = 8` (`errManifest`) | Статус ошибки зафиксирован, запись заблокирована | PASS |
| **GETSTATUS readable** | Запрос состояния `0xA1/3` возвращает 6-байтовую структуру DFU-статуса | `bStatus == STATUS_OK (0)` | PASS |
| **Stall tolerance on status** | Хостовый загрузчик завершает передачу, даже если IN-стадия (`0xC0/0`) аппаратно зависла | Образ передан полностью без фатального исключения | PASS |

Все 9 тестов отрабатывают за доли секунды без внешних зависимостей.
