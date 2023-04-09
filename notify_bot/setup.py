import os
from setuptools import setup, find_packages

from pip._internal.req import parse_requirements
from pip._internal.network.session import PipSession

current_dir = os.path.dirname(os.path.abspath(__file__))
requirements = parse_requirements(os.path.join(current_dir, "requirements.txt"), session=PipSession())
install_requires = [str(r.requirement) for r in requirements]

setup(
    name="notify_bot",
    version="0.1.0",
    include_package_data=True,
    zip_safe=False,
    install_requires=install_requires,
)
