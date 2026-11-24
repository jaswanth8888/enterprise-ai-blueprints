// Copyright (C) 2025-2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
// Loaded before the stylesheet renders, so a saved dark theme does not flash light first.
'use strict';
try {
  document.documentElement.dataset.theme = localStorage.getItem('sca_theme') === 'dark' ? 'dark' : 'light';
} catch (err) {
  document.documentElement.dataset.theme = 'light';
}
