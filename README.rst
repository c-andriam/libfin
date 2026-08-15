========
libfin
========
Libfin is a python package for working with payment card systems including
command line tools for working with Mastercard IPM files.

.. image:: https://img.shields.io/pypi/v/libfin.svg
        :target: https://pypi.org/project/libfin
        :alt: Version

.. image:: https://img.shields.io/pypi/wheel/libfin.svg
        :target: https://pypi.org/project/libfin
        :alt: Wheel

.. image:: https://img.shields.io/pypi/implementation/libfin.svg
        :target: https://pypi.org/project/libfin
        :alt: Implementation

.. image:: https://img.shields.io/pypi/dm/libfin.svg
        :target: https://pypi.org/project/libfin
        :alt: Downloads per month

.. image:: https://img.shields.io/pypi/pyversions/libfin.svg
        :target: https://pypi.org/project/libfin
        :alt: Python versions

.. image:: https://snyk.io/advisor/python/libfin/badge.svg
        :target: https://snyk.io/advisor/python/libfin
        :alt: snyk package health


Features
========
* ISO8583 message parsing
* Mastercard IPM file reader/writer/encoder
* Check digit calculator
* Encrypted pin block generator
* Visa PVV calculator

Installing
==========
Install and update using pip::

    pip install -U libfin


Information
===========
* Works with all supported Python versions.
* Pythonic programmer interfaces
* Core library has **zero** package dependencies.
* Low memory usage
* Download from `pypi <https://pypi.org/project/libfin/>`_
* Documentation available at `Read The Docs <https://libfin.readthedocs.io/en/latest/>`_

Acknowledgements
================
The python `hexdump` library is embedded in this package.
This library is a life saver for debugging issues with binary data.
Available at `Pypi:hexdump <https://pypi.org/project/hexdump/>`_.

The iso8583 module in libfin was inspired by the work of Igor V. Custodio from his
original ISO8583 parser. Available at `Pypi:ISO8583-Module <https://pypi.org/project/ISO8583-Module/>`_.

Mastercard is a registered trademark of Mastercard International Incorporated.
