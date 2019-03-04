#!/usr/bin/env python
""" SqlAlchemy queries with MongoDB-style """

from setuptools import setup, find_packages

setup(
    name='mongosql',
    version='1.5.2',
    author='Mark Vartanyan',
    author_email='kolypto@gmail.com',

    url='https://github.com/kolypto/py-mongosql',
    license='BSD',
    description=__doc__,
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    keywords=['sqlalchemy'],

    packages=find_packages(exclude=('tests',)),
    scripts=[],
    entry_points={},

    install_requires=[
        'sqlalchemy >= 0.9.7',
        'future',
    ],
    extras_require={},
    include_package_data=True,
    test_suite='nose.collector',

    platforms='any',
    classifiers=[
        # https://pypi.python.org/pypi?%3Aaction=list_classifiers
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'Natural Language :: English',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 3',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ],
)
