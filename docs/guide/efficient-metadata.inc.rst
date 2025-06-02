.. _efficient-metadata:

Efficient Metadata Handling
===========================

``tmt`` offers several features to help you manage your test metadata efficiently, reducing duplication and making it easier to maintain large test suites. This chapter highlights some of these key features.

Minimize Duplication
~~~~~~~~~~~~~~~~~~~~

Leverage the following features to avoid repetition in your metadata:

*   **Inheritance**: The Flexible Metadata Format (fmf) allows metadata to be inherited from parent directories. This is a powerful way to define common attributes at a higher level, which are then automatically picked up by tests and plans in subdirectories. For a detailed explanation, see the :ref:`inheritance` section.

*   **YAML Anchors and Aliases**: For repetition within a single YAML file, you can use `YAML anchors and aliases <anchors-aliases_>`_. This allows you to define a chunk of YAML once and reuse it multiple times within the same file. Learn more about this in the :ref:`anchors-aliases` section.

Adapt to Context
~~~~~~~~~~~~~~~~

*   **Adjust**: The :ref:`/spec/core/adjust` attribute provides a flexible way to modify metadata based on the current context (e.g., different distributions, architectures, or environments). This allows you to tailor tests and plans without duplicating the entire metadata structure. For example, you can adjust test requirements or enable/disable tests based on specific conditions.

Share Tests Across Repositories
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``tmt`` allows you to discover and execute tests that reside in different Git repositories. This is particularly useful for sharing common test libraries or when tests are maintained by different teams.

To include tests from an external repository, specify the ``url`` and optionally a ``ref`` (branch, tag, or commit) in the ``discover`` step of your plan:

.. code-block:: yaml

    discover:
      how: fmf
      url: https://github.com/teemtee/tmt.git
      ref: main
      # You can also specify a path within the repository
      # path: /tests/core

This will clone the specified repository and discover tests according to the fmf metadata found there. This enables modular test organization and promotes reusability of test code across projects. You can also use local paths to other repositories if they are available on the same filesystem.
For general information on configuring test discovery, see :ref:`/spec/plans/discover`.

By utilizing these features, you can create a more maintainable, scalable, and efficient test metadata structure.
