const assert = require('assert');
const fs = require('fs');
const path = require('path');

const indexHtml = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
const lightboxJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'lightbox.js'), 'utf8');
const slotCardJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'slot_card.js'), 'utf8');

// 1. Check DOM structure and element IDs in index.html
assert.ok(indexHtml.includes('id="candidate-selection-modal"'), 'Modal container must exist');
assert.ok(!indexHtml.includes('<div class="modal" id="candidate-selection-modal" style="display:none;"'),
    'Modal must not have hardcoded style="display:none;" on container which breaks active transitions');
assert.ok(indexHtml.includes('id="candidate-modal-title"'), 'candidate-modal-title must exist');
assert.ok(indexHtml.includes('id="candidate-modal-seq"'), 'candidate-modal-seq must exist');
assert.ok(indexHtml.includes('id="candidate-modal-chosen-tag"'), 'candidate-modal-chosen-tag must exist');
assert.ok(indexHtml.includes('id="candidate-modal-reason"'), 'candidate-modal-reason must exist');
assert.ok(indexHtml.includes('id="candidates-modal-grid"'), 'candidates-modal-grid must exist');
assert.ok(indexHtml.includes('id="candidate-selection-close-btn"'), 'candidate-selection-close-btn must exist');

// 2. Check function definitions in app.js
assert.ok(appJs.includes('function openCandidateSelectionModal'), 'openCandidateSelectionModal must be defined');
assert.ok(appJs.includes('function closeCandidateSelectionModal'), 'closeCandidateSelectionModal must be defined');
assert.ok(appJs.includes('function switchCandidateForFrame'), 'switchCandidateForFrame must be defined');
assert.ok(appJs.includes('window.openCandidateSelectionModal = openCandidateSelectionModal'), 'Must expose on window');
assert.ok(appJs.includes("modal.classList.add('active')"), 'openCandidateSelectionModal must add active class');
assert.ok(appJs.includes("modal.classList.remove('active')"), 'closeCandidateSelectionModal must remove active class');

// 3. Check lightbox resilience in js/lightbox.js
assert.ok(lightboxJs.includes("typeof items === 'string'"), 'openLightbox must support string URL arguments');

// 5. Check model categorization and filtering support
assert.ok(indexHtml.includes('id="candidate-model-filter-bar"'), 'candidate-model-filter-bar must exist in index.html');
assert.ok(appJs.includes('getCandidateModelInfo'), 'getCandidateModelInfo helper must exist in app.js');
assert.ok(appJs.includes('candidate-model-filter-bar'), 'candidate-model-filter-bar handling must exist in app.js');
assert.ok(appJs.includes('activeModelFilter'), 'activeModelFilter must exist in app.js');

console.log('Candidate selection modal unit tests passed successfully!');
