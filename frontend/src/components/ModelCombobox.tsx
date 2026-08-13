import { useEffect, useRef, useState } from "react";

export type ModelOption = { value: string; label: string };

export function ModelCombobox({
  id,
  value,
  options,
  placeholder = "默认 / 自定义 ID",
  onChange,
  onCommit,
}: {
  id: string;
  value: string;
  options: ModelOption[];
  placeholder?: string;
  onChange: (value: string) => void;
  onCommit: (value: string) => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [filtering, setFiltering] = useState(false);
  const [highlighted, setHighlighted] = useState(-1);
  const query = filtering ? value.trim().toLocaleLowerCase() : "";
  const selectedLabel = value ? options.find((option) => option.value === value)?.label : undefined;
  const displayValue = filtering ? value : selectedLabel ?? value;
  const filteredOptions = options.filter((option) => {
    if (!query) return true;
    return option.value.toLocaleLowerCase().includes(query) || option.label.toLocaleLowerCase().includes(query);
  });

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  function selectOption(next: string) {
    onChange(next);
    onCommit(next);
    setOpen(false);
    setFiltering(false);
    setHighlighted(-1);
    inputRef.current?.focus({ preventScroll: true });
  }

  return (
    <div ref={rootRef} className="relative w-44">
      <input
        ref={inputRef}
        id={id}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls={`${id}-options`}
        aria-activedescendant={highlighted >= 0 ? `${id}-option-${highlighted}` : undefined}
        className="w-full rounded-md border border-dh-bsoft bg-dh-surface py-1 pl-2 pr-7 text-sm text-dh-text outline-none transition focus:border-dh-accent focus:ring-1 focus:ring-dh-accent/30"
        value={displayValue}
        placeholder={placeholder}
        onFocus={() => {
          setOpen(true);
          setFiltering(false);
        }}
        onChange={(event) => {
          onChange(event.target.value);
          setOpen(true);
          setFiltering(true);
          setHighlighted(-1);
        }}
        onBlur={() => {
          onCommit(value);
          window.setTimeout(() => {
            if (!rootRef.current?.contains(document.activeElement)) {
              setOpen(false);
              setFiltering(false);
            }
          }, 0);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
            if (!open) setFiltering(false);
            setHighlighted((current) => Math.min(current + 1, filteredOptions.length - 1));
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            setHighlighted((current) => Math.max(current - 1, 0));
          } else if (event.key === "Enter") {
            event.preventDefault();
            if (open && highlighted >= 0 && filteredOptions[highlighted]) {
              selectOption(filteredOptions[highlighted].value);
            } else {
              onCommit(value);
              setOpen(false);
              setFiltering(false);
              event.currentTarget.blur();
            }
          } else if (event.key === "Escape") {
            event.preventDefault();
            setOpen(false);
            setFiltering(false);
          }
        }}
      />
      <button
        type="button"
        tabIndex={-1}
        className="absolute inset-y-0 right-0 grid w-7 place-items-center rounded-r-md text-[10px] text-slate-400 hover:bg-dh-hover hover:text-slate-50"
        aria-label={open ? "收起模型选项" : "展开模型选项"}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => {
          setOpen((current) => !current);
          setFiltering(false);
          setHighlighted(-1);
          inputRef.current?.focus({ preventScroll: true });
        }}
      >
        <span className={`transition-transform ${open ? "rotate-180" : ""}`}>▼</span>
      </button>
      {open && (
        <div
          id={`${id}-options`}
          role="listbox"
          className="absolute left-0 top-[calc(100%+0.3rem)] z-[80] max-h-56 min-w-full overflow-auto rounded-lg border border-dh-bsoft bg-dh-surface p-1 shadow-xl shadow-black/30"
        >
          {filteredOptions.length > 0 ? filteredOptions.map((option, index) => {
            const selected = option.value === value;
            const active = index === highlighted;
            return (
              <button
                key={option.value || "default"}
                id={`${id}-option-${index}`}
                type="button"
                role="option"
                aria-selected={selected}
                className={`flex w-full items-center justify-between gap-3 rounded-md px-2.5 py-1.5 text-left text-xs ${
                  selected
                    ? "bg-slate-400/10 font-medium text-dh-tsoft"
                    : active
                      ? "bg-dh-s2 text-dh-text"
                      : "text-dh-tsoft hover:bg-dh-hover"
                }`}
                onMouseEnter={() => setHighlighted(index)}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => selectOption(option.value)}
              >
                <span className="whitespace-nowrap">{option.label}</span>
                {selected && <span className="text-dh-accent">✓</span>}
              </button>
            );
          }) : (
            <div className="px-2.5 py-2 text-xs text-slate-400">按 Enter 使用自定义模型 ID</div>
          )}
        </div>
      )}
    </div>
  );
}
