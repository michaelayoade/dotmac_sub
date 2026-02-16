/**
 * Alpine.js reusable file upload component.
 *
 * Usage:
 *   <div x-data='fileUpload({ maxSizeMB: 5, accept: "image/*", preview: "image", inputName: "logo_file" })'>
 *
 * Config options:
 *   maxSizeMB  — max file size in MB (default 10)
 *   accept     — HTML accept attribute value (default "")
 *   preview    — "image" | "icon" | "none" (default "none")
 *   label      — drop zone label text
 *   inputName  — name attribute for the hidden file input
 */
document.addEventListener('alpine:init', () => {
  Alpine.data('fileUpload', (config = {}) => ({
    maxSizeMB: config.maxSizeMB || 10,
    accept: config.accept || '',
    previewMode: config.preview || 'none',
    label: config.label || 'Drop file here or click to browse',
    inputName: config.inputName || 'file',

    // State
    fileName: '',
    fileSize: 0,
    previewUrl: '',
    isDragging: false,
    hasFile: false,
    errorMessage: '',

    get fileSizeDisplay() {
      if (this.fileSize < 1024) return this.fileSize + ' B';
      if (this.fileSize < 1024 * 1024) return (this.fileSize / 1024).toFixed(1) + ' KB';
      return (this.fileSize / (1024 * 1024)).toFixed(1) + ' MB';
    },

    handleFiles(files) {
      if (!files || files.length === 0) return;

      const file = files[0];
      this.errorMessage = '';

      // Client-side size check
      const maxBytes = this.maxSizeMB * 1024 * 1024;
      if (file.size > maxBytes) {
        this.errorMessage = 'File too large. Maximum size: ' + this.maxSizeMB + 'MB';
        this.$dispatch('show-toast', {
          message: this.errorMessage,
          type: 'error',
        });
        return;
      }

      this.fileName = file.name;
      this.fileSize = file.size;
      this.hasFile = true;

      // Transfer file to the real input via DataTransfer
      const dt = new DataTransfer();
      dt.items.add(file);
      this.$refs.fileInput.files = dt.files;

      // Image preview
      if (this.previewMode === 'image' || this.previewMode === 'icon') {
        if (file.type.startsWith('image/')) {
          const reader = new FileReader();
          reader.onload = (e) => {
            this.previewUrl = e.target.result;
          };
          reader.readAsDataURL(file);
        } else {
          this.previewUrl = '';
        }
      }
    },

    onDragOver(e) {
      e.preventDefault();
      this.isDragging = true;
    },

    onDragLeave(e) {
      e.preventDefault();
      this.isDragging = false;
    },

    onDrop(e) {
      e.preventDefault();
      this.isDragging = false;
      this.handleFiles(e.dataTransfer.files);
    },

    onInputChange(e) {
      this.handleFiles(e.target.files);
    },

    clearFile() {
      this.fileName = '';
      this.fileSize = 0;
      this.previewUrl = '';
      this.hasFile = false;
      this.errorMessage = '';
      this.$refs.fileInput.value = '';
    },
  }));
});
