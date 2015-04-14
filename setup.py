from setuptools import setup, find_packages

dependency_base_url = 'git+https://github.com/BD2KGenomics/'

setup(
    name="cgcloud-spark",
    version="1.0.dev1",
    package_dir={ '': 'src' },
    packages=find_packages( 'src' ),
    include_package_data=True,
    install_requires=[
        'cgcloud-lib>=1.0.dev1',
        'cgcloud-core>=1.0.dev1',
        'Fabric>=1.7.0',
        'lxml>=3.2.1'
    ],
    namespace_packages=[ 'cgcloud' ],
    dependency_links=[
        dependency_base_url + 'cgcloud-lib.git@master#egg=cgcloud-core-1.0.dev1',
        dependency_base_url + 'cgcloud-lib.git@master#egg=cgcloud-lib-1.0.dev1'
    ] )
