from pathlib import Path

from mopidy import config, ext

__version__ = "0.1.0"


class Extension(ext.Extension):
    dist_name = "Mopidy-Platinum"
    ext_name = "platinum"
    version = __version__

    def get_default_config(self):
        return config.read(Path(__file__).parent / "ext.conf")

    def get_config_schema(self):
        schema = super().get_config_schema()
        schema["refresh_interval"] = config.Integer(minimum=0, maximum=300)
        schema["status_refresh_interval"] = config.Integer(minimum=0, maximum=60)
        schema["max_list_items"] = config.Integer(minimum=1)
        schema["airplay_device_name"] = config.String(optional=True)
        return schema

    def setup(self, registry):
        from mopidy_platinum.frontend import factory

        registry.add("http:app", {"name": self.ext_name, "factory": factory})
