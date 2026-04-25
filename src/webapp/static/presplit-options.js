// 预分割选项交互逻辑
(function() {
  const presplitModeSelect = document.querySelector('#import-presplit-mode');
  const presplitSegmentsField = document.querySelector('#presplit-segments-field');
  
  if (presplitModeSelect && presplitSegmentsField) {
    presplitModeSelect.addEventListener('change', function() {
      if (this.value === 'custom') {
        presplitSegmentsField.style.display = 'block';
      } else {
        presplitSegmentsField.style.display = 'none';
      }
    });
  }
})();
