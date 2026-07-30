from .base import DataProvider
from .mt5_provider import MT5Provider
from .oanda_provider import OandaProvider
from .csv_provider import CsvProvider
from .moex_provider import MoexProvider


def create_provider(config) -> DataProvider:
    """Factory: create the right data provider based on config."""
    provider_name = getattr(config, "data_provider", "mt5")

    if provider_name == "mt5":
        return MT5Provider(
            login=config.mt5_login,
            password=config.mt5_password,
            server=config.mt5_server,
            timezone=config.timezone,
        )
    elif provider_name == "oanda":
        return OandaProvider(
            api_token=config.oanda_api_token,
            account_id=config.oanda_account_id,
            environment=config.oanda_environment,
            timezone=config.timezone,
        )
    elif provider_name == "csv":
        suffix = getattr(config, "csv_filename_suffix", "")
        return CsvProvider(data_dir="data", filename_suffix=suffix)
    elif provider_name == "moex":
        market = getattr(config, "moex_market", "forts")
        return MoexProvider(market=market, timezone=config.timezone)
    else:
        raise ValueError(f"Unknown data provider: {provider_name}")
