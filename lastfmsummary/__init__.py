async def setup(bot):
    from .cog import LastFMSummary

    await bot.add_cog(LastFMSummary(bot))
