"""Release-matrix system tests.

The report getters in :mod:`nornir_srl.connections` hard-code gNMI paths and the
YANG structure they expect back. Both change between SR Linux releases, so a
getter that works on one release can silently return nothing on another.

:mod:`tests.system.capture` records the raw gNMI exchange of every report
against a live fabric, one recording per node per release. :mod:`tests.system.replay`
feeds those recordings back through the real getters, which is what
``tests/test_release_matrix.py`` asserts against - so the release matrix is
checked on every test run, with no lab required.
"""
