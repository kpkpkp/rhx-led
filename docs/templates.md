# Templates from your own RHX Plus exports

Picture programs carry a block of controller settings (scan timing, panel size, work mode, a checksum) that we copy
from a program the vendor app made for **your** panel, then replace the pixels. Text programs are built the same way
from a seven-colour text program. These files come from the vendor's software, so each user makes their own.

## Make the exports (RHX Plus for Windows)

1. Set the screen to your panel's real size (e.g. 128×32). A width smaller than the panel makes the controller
   repeat parts of the picture across the extra columns.
2. For each mode you want: gear → **Setting** → **WorkMode** → choose the mode → back. Add a picture (or, for the
   text template, a text item in *Seven colors background*) filling the whole screen. Remove any program background.
3. Press **USB drive**. The app writes the program to `C:\LedRom`.
4. Import it before making the next one:

   ```
   python -m signboard.import_ledrom C:\LedRom
   ```

   This writes `signboard/templates/<mode>.bin` (or the folder set as `templates_dir`).

For **left/right pans** also export one picture with ShowWay **Left_Continue**: it uses a different (raw) layout
and is saved as `raw-<mode>.bin`.

| Template | Used by |
|---|---|
| `text.bin` | text programs (`signd` text jobs) |
| `seven-lengthen.bin`, `low-gray.bin`, `middle-gray.bin`, `high-gray.bin`, `true-color.bin` | pictures, sequences, patterns, up/down pans |
| `raw-low-gray.bin` | left/right pans |

`python -m signboard.selftest` checks every template it finds.
