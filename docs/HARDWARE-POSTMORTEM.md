# Аппаратный анализ сбоев USB-хостов на чипах Apple A7 (Hardware Post-Mortem)

## Введение

При использовании эксплойта **checkm8** на процессорах архитектуры Apple A7 (S5L8960X: iPad Air 1, iPad mini 2, iPhone 5s) в среде Linux инженеры регулярно сталкиваются с серией ошибок:
- `device descriptor read/64, error -110` (таймаут ETIMEDOUT при чтении дескриптора);
- `DFUSyncUpload FAILED: -1` (зависание отправки рамдиска);
- Циклический уход устройства в обычный reboot вместо режима `pwned DFU`.

В трекере `checkra1n/bugtracker` эта проблема зарегистрирована как **Issue #1169** и годами помечалась как «A7 Linux issue». Данный документ описывает физические замеры и причины происходящего.

---

## 1. Отказ физического сброса шины (USB Bus Reset Failure)

### Механика SecureROM
Для срабатывания уязвимости `checkm8` на этапе триггера требуется, чтобы шина перешла в состояние Reset при наличии активного асинхронного IN-трансфера. В штатном сценарии хост выдаёт команду сброса шины (длительность импульса SE0 не менее 10–20 мс).

### Особенность хост-контроллеров ASMedia (ASM1142, ASM2142, ASM3142)
Внешние PCIe-to-USB контроллеры ASMedia при вызове `libusb_reset_device()`:
1. Выполняют полный сброс внутреннего конечного автомата порта (Port Reset) с отключением VBUS/линка на время, превышающее допустимое окном ожидания SecureROM.
2. В результате тактовый генератор и память SRAM чипа A7 сбрасываются по аппаратному watchdog, и iPad уходит в полную перезагрузку (появляется логотип Apple).

### Решение: Soft-Reset через DFU ABORT
Вместо аппаратного сброса порта на шину отправляется управляющий DFU-запрос:
```
bmRequestType: 0x21 (Class, Interface, Host-to-Device)
bRequest:      0x06 (DFU_ABORT)
wValue:        0x0000
wIndex:        0x0000
wLength:       0
```
Внедрённый шеллкод в SecureROM перехватывает обработчик запросов интерфейса и интерпретирует `DFU_ABORT` как триггер шинного сброса, сохраняя контекст исполнения и переводя устройство в `PWND:[checkm8]` без физического обесточивания линии.

---

## 2. Зависание Bulk OUT Endpoint (Bulk Pipe Hang)

### Проблема в stock pongoOS
Официальный загрузчик pongoOS ожидает передачу исполняемого ядра и рамдиска исключительно через Bulk OUT Endpoint `0x02`:
```c
// pongoOS: src/shell/usbloader.c
if (setup->bRequest == 1 && setup->wLength == 4) {
    loader_xfer_size = *(uint32_t*)cmd_buf;
    loader_xfer_recv_data = alloc_static(loader_xfer_size);
    usb_out_transfer_dma(2, loader_xfer_recv_data, ...); // Endpoint 2 (Bulk)
    return true;
}
```

На многих xHCI-контроллерах в Linux после выполнения низкоуровневых манипуляций дескриптор конечной точки Bulk `0x02` перестает отвечать хосту (pipe stall или silence). Хостовая утилита checkra1n выдает `DFUSyncUpload FAILED: -1`, так как `libusb_bulk_transfer` завершается по таймауту.

### Решение «Шнурка»: Транспорт через Endpoint 0
Endpoint 0 (Control) обслуживается аппаратно контроллером Synopsys OTG независимо от состояния Bulk endpoints. Патч перенаправляет поток данных в DFU-запрос `0x21/1`:
- `wValue` передает порядковый номер чанка (1..65535);
- `wLength` ограничен размером буфера DMA (`0x1000` = 4096 байт);
- Данные пишутся напрямую в `loader_xfer_recv_data` со сбросом кэша `cache_invalidate()`.

---

## 3. Конфликт с ядром: `apple-mfi-fastcharge`

В современных ядрах Linux (5.x, 6.x, 7.x) присутствует модуль ядра `apple-mfi-fastcharge`, который автоматически биндится на интерфейс 0 любого USB-устройства с Vendor ID `0x05ac`.

При попытке открыть устройство в режиме DFU через `libusb_claim_interface(handle, 0)` хост получает ошибку `LIBUSB_ERROR_BUSY (-6)`.

### Решение
Отвязка драйвера через sysfs перед инициализацией USB-трансфера:
```bash
echo -n "$DEVICE_ID" > /sys/bus/usb/drivers/apple-mfi-fastcharge/unbind
```
Или автоматическое правило udev:
```udev
SUBSYSTEM=="usb", ATTR{idVendor}=="05ac", ATTR{idProduct}=="1227", ACTION=="add", \
    RUN+="/bin/sh -c 'echo -n %k > /sys/bus/usb/drivers/apple-mfi-fastcharge/unbind 2>/dev/null || true'"
```
