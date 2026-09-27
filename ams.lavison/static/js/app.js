document.addEventListener("DOMContentLoaded", () => {
  const hidInput = document.getElementById("hidReaderInput");
  const simulateButton = document.getElementById("simulateScan");
  const simulatedUid = document.getElementById("simulatedUid");
  const courseSelect = document.getElementById("courseSelect");
  const sessionSelect = document.getElementById("sessionSelect");
  const courseIdInput = document.getElementById("courseIdInput");

  if (courseSelect && courseIdInput) {
    const params = new URLSearchParams(window.location.search);
    const currentCourse = params.get("course_id");
    if (currentCourse) {
      courseSelect.value = currentCourse;
      courseIdInput.value = currentCourse;
    }
  }

  if (sessionSelect && courseIdInput) {
    sessionSelect.addEventListener("change", () => {
      const form = document.getElementById("attendanceForm");
      if (form) {
        const courseValue =
          courseIdInput.value || (courseSelect && courseSelect.value) || "";
        if (courseValue && sessionSelect.value) {
          form.submit();
        }
      }
    });
  }

  if (hidInput) {
    hidInput.focus();
    hidInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        const value = hidInput.value.trim();
        if (value.length >= 6) {
          event.preventDefault();
          hidInput.value = value.toUpperCase();
          hidInput.closest("form")?.submit();
        }
      }
    });
  }

  if (simulateButton && simulatedUid && hidInput) {
    simulateButton.addEventListener("click", () => {
      const value = simulatedUid.value.trim();
      if (!value) {
        window.alert("Please enter a test UID first.");
        return;
      }

      hidInput.value = value.toUpperCase();
      const form = hidInput.closest("form");
      if (form) {
        form.submit();
      }
    });
  }

  const scanNfcBtn = document.getElementById("scanNfcBtn");
  const manualUid = document.getElementById("manualUid");
  const focusReaderBtn = document.getElementById("focusReaderBtn");

  if (scanNfcBtn && manualUid) {
    scanNfcBtn.addEventListener("click", () => {
      manualUid.value = "04A1B2C3D4";
      manualUid.focus();
    });
  }

  if (focusReaderBtn && manualUid) {
    focusReaderBtn.addEventListener("click", () => {
      manualUid.focus();
      manualUid.placeholder = "Enter UID manually";
    });
  }
});
