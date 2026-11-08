"""CLI parser."""

import argparse


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--shout", action="store_true")
    return parser
