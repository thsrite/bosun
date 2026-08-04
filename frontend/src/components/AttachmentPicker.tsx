import { useRef, type ReactNode } from "react";

export function AttachmentPicker({
  accept,
  buttonClassName,
  children,
  disabled,
  multiple = true,
  onFiles,
  title,
}: {
  accept?: string;
  buttonClassName: string;
  children: ReactNode;
  disabled?: boolean;
  multiple?: boolean;
  onFiles: (files: File[]) => void | Promise<void>;
  title?: string;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        multiple={multiple}
        className="hidden"
        onChange={(event) => {
          const files = Array.from(event.currentTarget.files ?? []);
          event.currentTarget.value = "";
          if (files.length > 0) void onFiles(files);
        }}
      />
      <button
        type="button"
        className={buttonClassName}
        disabled={disabled}
        onClick={() => inputRef.current?.click()}
        title={title}
      >
        {children}
      </button>
    </>
  );
}
