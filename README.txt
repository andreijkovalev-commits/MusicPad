MusicPad — музыка в микрофон по горячей клавише (как Soundpad)

Как это работает: MusicPad перехватывает звук выбранного приложения
(например, Яндекс Музыки) и по горячей клавише подмешивает его в
виртуальный микрофон. Discord или игра слушают этот микрофон.
Музыка при этом продолжает играть у вас в обычные наушники, ничего
перенаправлять в микшере Windows не нужно.

Единственное, что нужно поставить отдельно — бесплатный виртуальный
микрофон VB-Cable. Создать виртуальное аудиоустройство без драйвера
невозможно, поэтому полностью обойтись без него нельзя.

=== 1. СБОРКА ПРИЛОЖЕНИЯ (один раз) ===
1. Установите Python 3.12 или 3.13 с python.org
   (галочки "Add Python to PATH" и "pip"). Для самых новых версий
   (3.15) могут не быть готовых сборок нужных модулей.
2. Положите рядом: musicpad_app.py, requirements.txt, build.bat.
3. Дважды кликните build.bat. Готовый файл: dist\MusicPad.exe
   (его можно переносить, Python на другом ПК не нужен).
   Без сборки: pip install -r requirements.txt, затем python musicpad_app.py

Для захвата звука приложения нужна Windows 10 (20H1+) или Windows 11.

=== 2. VB-CABLE (один раз) ===
1. Скачайте с vb-audio.com/Cable (в окне MusicPad есть кнопка).
2. Распакуйте, запустите VBCABLE_Setup_x64.exe от имени администратора.
3. Перезагрузите компьютер.

=== 3. ПЕРВЫЙ ЗАПУСК ===
1. Включите музыку в Яндекс Музыке.
2. Откройте MusicPad. В пункте «1» нажмите «Обновить» — в списке
   появится приложение (Яндекс Музыка). Выберите его.
3. В пункте «2» виртуальный микрофон выбирается сам (CABLE Input).
   «Мой микрофон» — ваш реальный микрофон, чтобы собеседники слышали и вас.
4. Нажмите «Запустить». Под списком приложения должна появиться
   надпись «Захват звука: ...», а индикатор уровня будет двигаться.
5. В Discord / игре выберите микрофон «CABLE Output» (подсказка
   с точным названием написана в окне MusicPad).
6. Горячая клавиша F8 включает музыку в микрофоне.

=== НАСТРОЙКИ DISCORD (важно!) ===
Параметры -> Голос и видео:
- выключите «Шумоподавление» (Krisp) и «Эхоподавление»;
- выключите «Автоматическую регулировку усиления»;
- «Автоматически определять чувствительность» выключите и поставьте
  порог низко.
Иначе Discord будет считать музыку шумом и вырезать её.
Для проверки можно нажать «Проверка микрофона» в тех же настройках.

=== ПОЛЕЗНО ЗНАТЬ ===
- Если музыка не появляется в списке приложений: убедитесь, что она
  играет прямо сейчас, и нажмите «Обновить». Имя процесса можно ввести
  вручную (например, "Яндекс Музыка.exe" — как в Диспетчере задач).
- Режим «Звук с устройства» — запасной вариант на случай, если захват
  приложения не работает: в нём нужно вручную направить плеер в
  CABLE Input через Параметры -> Звук -> Микшер громкости, а в MusicPad
  выбрать источник CABLE Output и второй виртуальный кабель для
  микрофона (VB-Cable A+B или Voicemeeter).
- Если игра запущена от администратора, запускайте MusicPad тоже от
  администратора, иначе горячие клавиши в ней не сработают.
- Трей: при сворачивании или закрытии окно уходит в трей. Значок:
  серый — остановлено, красный — музыка в микрофоне выкл, зелёный — ВКЛ.
  Двойной клик открывает окно, правый клик — меню. Полностью закрыть
  программу — пункт «Выход».
- Антивирус иногда ругается на exe, собранный PyInstaller: это ложное
  срабатывание, исходники открыты (musicpad_app.py).
- Настройки хранятся в %APPDATA%\MusicPad\config.json


=== ИСПРАВЛЕНИЕ PROC-TAP (v2) ===
В proc-tap 1.1.1 публичный класс называется ProcessAudioCapture.
Старый код импортировал ProcTap и StreamConfig, которых нет в текущем
экспорте пакета. Исправленный код использует:
    from proctap import ProcessAudioCapture
    ProcessAudioCapture(pid, on_data=callback)
Для пересборки запустите build.bat из этой папки.


=== VB-CABLE INSTALLATION ===
The build.bat script downloads the official VB-CABLE Driver Pack 45 from
https://vb-audio.com/Cable/ and places the extracted files in dist\VB-CABLE.
It does not install a kernel audio driver on the build computer automatically.

Distribute the complete dist folder. On each target PC, run
Install_VB-CABLE.bat, approve the Windows administrator prompt, and follow
the VB-CABLE setup window. Restart Windows after installation.

In MusicPad choose CABLE Input (playback/output endpoint). In Discord choose
CABLE Output (recording/input endpoint) as the microphone.
Official installation instructions require extracting the archive, running
the setup program as administrator, and rebooting after installation.
