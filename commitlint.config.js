// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2025 Scott Friedman and Project Contributors

module.exports = {
  extends: ['@commitlint/config-conventional'],
  rules: {
    // Type enum - allowed commit types
    'type-enum': [
      2,
      'always',
      [
        'build',     // Changes that affect the build system or external dependencies
        'ci',        // Changes to CI configuration files and scripts
        'docs',      // Documentation only changes
        'feat',      // A new feature
        'fix',       // A bug fix
        'perf',      // A code change that improves performance
        'refactor',  // A code change that neither fixes a bug nor adds a feature
        'style',     // Changes that do not affect the meaning of the code
        'test',      // Adding missing tests or correcting existing tests
        'chore',     // Other changes that don't modify src or test files
        'revert'     // Reverts a previous commit
      ]
    ],

    // Subject and body rules
    'subject-case': [2, 'never', ['pascal-case', 'upper-case']],
    'subject-empty': [2, 'never'],
    'subject-full-stop': [2, 'never', '.'],
    'subject-max-length': [2, 'always', 72],
    'body-leading-blank': [1, 'always'],
    'body-max-line-length': [2, 'always', 100],
    'footer-leading-blank': [1, 'always'],

    // Header rules
    'header-max-length': [2, 'always', 100],

    // Allow longer body lines for detailed commit messages
    'body-max-line-length': [1, 'always', 200],

    // Custom rules for this project
    'scope-enum': [
      1,
      'always',
      [
        // Core components
        'provider',
        'modes',
        'compute',
        'network',
        'state',
        'utils',

        // Specific modes
        'standard',
        'detached',
        'serverless',

        // AWS services
        'ec2',
        'lambda',
        'ecs',
        'spot',
        'vpc',
        'cloudformation',

        // Development
        'dev-env',
        'tests',
        'ci',
        'docs',
        'examples',
        'deps',

        // Meta
        'release'
      ]
    ]
  },

  // Allow longer commit messages for detailed explanations
  parserPreset: {
    parserOpts: {
      headerPattern: /^(\w*)(?:\(([^)]*)\))?: (.*)$/,
      headerCorrespondence: ['type', 'scope', 'subject']
    }
  }
};
