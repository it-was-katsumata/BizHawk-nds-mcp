[English](API_README.md) | العربية

# مرجع واجهة برمجة تطبيقات BizHawk MCP للتصحيح

> ⚠️ **تنبيه هام**: جميع الأدوات تعمل **فقط على حالة المحاكي المؤقتة في وقت التشغيل**. لا تقوم بتعديل الملفات أو كود الخادم أو الإعدادات أو النظام المضيف.

---

## أدوات الذاكرة

| الأداة | الوصف | المعاملات |
|--------|-------|-----------|
| `emu_debug_read_memory` | قراءة بايت من عنوان | `address`, `domain?` |
| `emu_debug_write_memory` | كتابة بايت إلى عنوان | `address`, `value`, `domain?` |
| `emu_debug_read_memory_range` | قراءة نطاق بايتات | `address`, `length`, `domain?` |
| `emu_debug_search_memory` | البحث عن قيمة في RAM | `value` |
| `emu_debug_snapshot_memory` | أخذ لقطة من RAM | `name?` |
| `emu_debug_compare_memory` | مقارنة RAM باللقطة | `name?`, `filter?` |
| `emu_debug_list_memory_domains` | عرض نطاقات الذاكرة المتاحة | — |

### نطاقات الذاكرة

| النطاق | الحجم | الوصف |
|--------|-------|-------|
| `System Bus` | 64KB | مساحة العناوين الكاملة (افتراضي) |
| `RAM` | 2KB | ذاكرة العمل |
| `PALRAM` | 32B | لوحة ألوان PPU |
| `OAM` | 256B | ذاكرة سمات الكائنات المتحركة |
| `PRG ROM` | متغير | ذاكرة البرنامج للقراءة فقط |
| `CHR` / `CHR VROM` | متغير | ذاكرة الرسومات للقراءة فقط |

---

## أدوات المعالج

| الأداة | الوصف | المعاملات |
|--------|-------|-----------|
| `emu_debug_get_cpu_state` | الحصول على حالة المعالج الكاملة مع الأعلام | — |
| `emu_debug_get_cpu_registers` | الحصول على السجلات بصيغة ست عشرية | — |

---

## التحكم بالتنفيذ

| الأداة | الوصف | المعاملات |
|--------|-------|-----------|
| `emu_debug_pause` | إيقاف المحاكي مؤقتاً | — |
| `emu_debug_resume` | استئناف المحاكي | — |
| `emu_debug_step` | التقدم بإطار أو تعليمة | `count?`, `stepType?` |
| `emu_debug_frame_advance` | التقدم بالإطارات | `count?` |

---

## نقاط التوقف

| الأداة | الوصف | المعاملات |
|--------|-------|-----------|
| `emu_debug_add_breakpoint` | إضافة نقطة توقف | `type`, `address` |
| `emu_debug_list_breakpoints` | عرض جميع نقاط التوقف | — |
| `emu_debug_remove_breakpoint` | إزالة نقطة توقف بالمعرف | `id` |
| `emu_debug_clear_breakpoints` | إزالة جميع نقاط التوقف | — |
| `emu_debug_get_breakpoint_hits` | الحصول على جميع إصابات نقاط التوقف | — |
| `emu_debug_get_last_breakpoint_hit` | الحصول على آخر إصابة | — |
| `emu_debug_clear_breakpoint_hits` | مسح قائمة الإصابات | — |
| `emu_debug_set_breakpoint_auto_pause` | تفعيل/تعطيل الإيقاف التلقائي | `enabled` |

---

## التفكيك والتتبع

| الأداة | الوصف | المعاملات |
|--------|-------|-----------|
| `emu_debug_disassemble` | تفكيك عند العنوان | `address`, `count?` |
| `emu_debug_get_current_instruction` | الحصول على التعليمة عند PC | — |
| `emu_debug_start_trace` | بدء تسجيل التتبع | — |
| `emu_debug_stop_trace` | إيقاف تسجيل التتبع | — |
| `emu_debug_get_trace` | الحصول على مدخلات التتبع | `count?` |
| `emu_debug_clear_trace` | مسح سجل التتبع | — |

---

## قائمة المراقبة

| الأداة | الوصف | المعاملات |
|--------|-------|-----------|
| `emu_debug_add_watch` | إضافة عنوان للمراقبة | `address`, `name?` |
| `emu_debug_list_watches` | الحصول على جميع القيم المراقبة | — |
| `emu_debug_remove_watch` | إزالة مراقبة | `address` |
| `emu_debug_clear_watches` | مسح جميع المراقبات | — |

---

## الحالة والإدخال

| الأداة | الوصف | المعاملات |
|--------|-------|-----------|
| `emu_debug_get_emulator_state` | الحصول على حالة المحاكي | — |
| `emu_debug_save_state` | حفظ حالة المحاكي | `slot?`, `path?` |
| `emu_debug_load_state` | تحميل حالة المحاكي | `slot?`, `path?` |
| `emu_debug_set_input` | تعيين إدخال وحدة التحكم | `buttons?`, `player?` |
| `emu_debug_raw_command` | إرسال أمر خام | `action`, `params?` |

---

## أدوات الغش (التجميد/القفل)

| الأداة | الوصف | المعاملات |
|--------|-------|-----------|
| `emu_cheat_freeze_address` | تجميد عنوان على قيمة ثابتة | `address`, `value`, `label?`, `domain?` |
| `emu_cheat_unfreeze_address` | إزالة التجميد | `freezeId?`, `address?`, `all?` |
| `emu_cheat_list_freezes` | عرض التجميدات النشطة | — |

---

## أدوات الغش عالية المستوى

| الأداة | الوصف | المعاملات |
|--------|-------|-----------|
| `emu_cheat_find_decrementing_value` | البحث عن قيم تتناقص مع الوقت | `initialValue?`, `frames?` |
| `emu_cheat_find_value_on_event` | باحث قيم من مرحلتين | `phase`, `snapshotName?`, `filter?` |
| `emu_cheat_narrow_candidates` | تضييق العناوين المرشحة | `addresses`, `action`, `filter?` |

---

## أدوات سير عمل التصحيح

| الأداة | الوصف | المعاملات |
|--------|-------|-----------|
| `emu_debug_capture_write_source` | مراقبة ما يكتب إلى عنوان | `address`, `frames?` |
| `emu_debug_trace_and_summarize` | تتبع وإيجاد نقاط التنفيذ الساخنة | `frames?`, `topN?` |

---

## أمثلة الاستخدام

### تغيير ألوان اللوحة
```
1. "اقرأ PALRAM من العنوان 0 إلى 31"
2. "اكتب القيمة 42 (أخضر) إلى عنوان PALRAM رقم 17"
```

### البحث عن عنوان ذاكرة
```
1. "ابحث في الذاكرة عن القيمة 3" (الحياة الأولية)
2. اخسر حياة، ثم "ابحث في الذاكرة عن القيمة 2"
3. "أضف نقطة توقف كتابة عند $075A"
```

### غش الحياة اللانهائية
```
1. "جمّد العنوان $075A على القيمة 9"
```

### تتبع التنفيذ
```
1. "أوقف المحاكي مؤقتاً"
2. "ابدأ تسجيل التتبع"
3. "تقدم إطار واحد"
4. "أرني سجل التتبع"
```

### أتمتة الإدخال
```
1. "اضبط الإدخال على ضغط زر A"
2. "تقدم 10 إطارات"
3. "امسح الإدخال"
```
